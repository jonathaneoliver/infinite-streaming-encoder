#!/usr/bin/env bash
# vm-floor-bench.sh — measure the REAL Docker-Desktop VM/toolchain tax on a Mac,
# separated from the x265-version confound.
#
# It encodes the SAME clip with the SAME x265 params two ways — natively (Homebrew
# ffmpeg) and inside the encoder container — and compares fps. Two measurements:
#
#   1) single-core (1 thread, pools=1): the FLAT per-instruction VM/codegen floor.
#   2) loaded (SLOTS encodes x 2 threads = the Mac's P-core config): adds P/E
#      scheduling.
#
# Read it like this:
#   * gap_loaded ~= gap_1t   -> a flat VM/codegen tax; near-irreducible.
#   * gap_loaded  >  gap_1t  -> the extra is E-core spill inside the VM (macOS
#                               can't give the VM's vCPU threads P-core QoS) —
#                               reducible by right-sizing Docker Desktop's vCPUs.
#   * the printed x265 VERSIONS differ -> part of any gap is version, not the VM
#     (same confound that turned a "2x" machine gap into 1.35x). Match them to
#     trust the floor.
#
# The encode reads a fixed clip and writes to `-f null -` (no output I/O) so the
# comparison is pure compute. Container input is mounted read-only; that one
# sequential read is negligible against a multi-second encode.
#
# Run on a Mac (MacBook or Mac Mini). Requires:
#   * native ffmpeg with libx265   (brew install ffmpeg)
#   * the encoder image present     (IMAGE, default encoder:latest)
#
# Knobs (env): IMAGE, SRC (a real source is more representative), DUR seconds,
# SLOTS (loaded concurrency; Macs = 2), PRESET.
set -u

IMAGE="${IMAGE:-encoder:latest}"
SRC="${SRC:-}"
DUR="${DUR:-30}"
SLOTS="${SLOTS:-2}"
PRESET="${PRESET:-medium}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
IN="$WORK/in.mp4"

command -v ffmpeg >/dev/null 2>&1 || { echo "no native ffmpeg — 'brew install ffmpeg'" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image '$IMAGE' not found — set IMAGE or 'make build'" >&2; exit 1; }

# --- fixed input, identical for both runs -----------------------------------
# HEIGHT (optional): downscale the prepared clip to this height (e.g. 1080) to
# match the 1080p-per-core methodology / keep 4K sources fast. Unset = native.
SCALE_VF=""
[ -n "${HEIGHT:-}" ] && SCALE_VF="-vf scale=-2:${HEIGHT}"
if [ -n "$SRC" ]; then
  echo "using source: $SRC (first ${DUR}s${HEIGHT:+, scaled to ${HEIGHT}p})"
  ffmpeg -hide_banner -loglevel error -y -t "$DUR" -i "$SRC" $SCALE_VF \
    -c:v libx264 -preset veryfast -crf 18 -an "$IN"
else
  echo "no SRC given — synthesizing a ${DUR}s 1080p clip (pass SRC=/path for real content)"
  ffmpeg -hide_banner -loglevel error -y -f lavfi \
    -i "testsrc2=size=1920x1080:rate=30:duration=$DUR" \
    -pix_fmt yuv420p -c:v libx264 -preset veryfast -crf 18 "$IN"
fi

# --- one encode; echoes "<fps> <x265ver>" parsed from -benchmark stderr ------
# $1=mode(native|container)  $2=threads  $3=pools  $4=stderr-file
run_one() {
  mode="$1"; threads="$2"; pools="$3"; errf="$4"
  xp="pools=${pools}:frame-threads=1:log-level=none"
  if [ "$mode" = native ]; then
    "${NATIVE_FF:-ffmpeg}" -hide_banner -benchmark -y -i "$IN" \
      -c:v libx265 -preset "$PRESET" -x265-params "$xp" -threads "$threads" \
      -an -f null - >/dev/null 2>"$errf"
  else
    docker run --rm -v "$WORK:/w:ro" --entrypoint ffmpeg "$IMAGE" \
      -hide_banner -benchmark -y -i /w/in.mp4 \
      -c:v libx265 -preset "$PRESET" -x265-params "$xp" -threads "$threads" \
      -an -f null - >/dev/null 2>"$errf"
  fi
}

# fps = encoded frames / x265 rtime (both excl. container startup, from -benchmark)
fps_of() {
  f="$1"
  frames=$(grep -oE 'frame= *[0-9]+' "$f" | tail -1 | grep -oE '[0-9]+' | tail -1)
  rtime=$(grep -oE 'rtime=[0-9.]+' "$f" | tail -1 | cut -d= -f2)
  [ -n "$frames" ] && [ -n "$rtime" ] || { echo ""; return; }
  awk -v n="$frames" -v t="$rtime" 'BEGIN{ if (t>0) printf "%.2f", n/t; }'
}
x265ver_of() { grep -oE 'x265 .info.: HEVC .* version [^ ]+' "$1" | head -1 | grep -oE 'version .*' ; }

echo
echo "=== Test 1: single-core (1 thread, pools=1) — the flat VM/codegen floor ==="
run_one native    1 1 "$WORK/n1.err"
run_one container 1 1 "$WORK/c1.err"
NF1=$(fps_of "$WORK/n1.err"); CF1=$(fps_of "$WORK/c1.err")
echo "  native    : ${NF1:-?} fps   [$(x265ver_of "$WORK/n1.err")]"
echo "  container : ${CF1:-?} fps   [$(x265ver_of "$WORK/c1.err")]"
G1=$(awk -v n="$NF1" -v c="$CF1" 'BEGIN{ if (c>0) printf "%.0f", (n/c-1)*100; }')
echo "  container is ${G1:-?}% slower per core"

echo
echo "=== Test 2: loaded ($SLOTS encodes x 2 threads = your P-core config) ==="
# launch SLOTS concurrent encodes per mode; average their per-instance fps.
avg_loaded() {
  mode="$1"; i=1
  while [ "$i" -le "$SLOTS" ]; do
    run_one "$mode" 2 2 "$WORK/${mode}_L$i.err" &
    i=$((i+1))
  done
  wait
  i=1; sum=0; cnt=0
  while [ "$i" -le "$SLOTS" ]; do
    v=$(fps_of "$WORK/${mode}_L$i.err")
    [ -n "$v" ] && { sum=$(awk -v s="$sum" -v v="$v" 'BEGIN{printf "%.4f", s+v}'); cnt=$((cnt+1)); }
    i=$((i+1))
  done
  [ "$cnt" -gt 0 ] && awk -v s="$sum" -v c="$cnt" 'BEGIN{printf "%.2f", s/c}' || echo ""
}
NL=$(avg_loaded native); CL=$(avg_loaded container)
NAGG=$(awk -v v="$NL" -v s="$SLOTS" 'BEGIN{printf "%.1f", v*s}')
CAGG=$(awk -v v="$CL" -v s="$SLOTS" 'BEGIN{printf "%.1f", v*s}')
echo "  native    : ${NL:-?} fps/encode  -> ${NAGG:-?} fps aggregate"
echo "  container : ${CL:-?} fps/encode  -> ${CAGG:-?} fps aggregate"
GL=$(awk -v n="$NL" -v c="$CL" 'BEGIN{ if (c>0) printf "%.0f", (n/c-1)*100; }')
echo "  container is ${GL:-?}% slower under load"

echo
echo "=== Verdict ==="
echo "  single-core tax : ${G1:-?}%   loaded tax : ${GL:-?}%"
awk -v g1="${G1:-0}" -v gl="${GL:-0}" 'BEGIN{
  d = gl - g1;
  if (d >= 8) print "  loaded >> single-core: the extra ~"d"pts is E-core spill in the VM";
  else if (d <= -8) print "  loaded << single-core: load actually closes the gap (scheduling favors P under load)";
  else print "  loaded ~= single-core: a flat VM/codegen tax, near-irreducible";
}'
echo "  (if the two x265 versions above differ, subtract the version delta before"
echo "   trusting these as pure VM tax — match versions to isolate the floor.)"
