#!/usr/bin/env bash
# Preflight config check ("make doctor"): validate .env / host tools / per-target
# config and report OK / WARN / FAIL clearly, so misconfiguration is caught up
# front instead of failing cryptically at run time. Exit non-zero on any FAIL.
#
# Reads config from the environment (the Makefile exports .env). Can also be run
# directly after sourcing a .env:  set -a; . .env; set +a; bash scripts/doctor.sh
set -u

if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; N=$'\033[0m'
else G=; Y=; R=; B=; N=; fi
fails=0; warns=0
ok(){   printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn(){ printf '  %s⚠%s %s\n' "$Y" "$N" "$1"; warns=$((warns+1)); }
bad(){  printf '  %s✗%s %s\n' "$R" "$N" "$1"; fails=$((fails+1)); }
section(){ printf '\n%s%s%s\n' "$B" "$1" "$N"; }

section "Host tools"
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then ok "docker daemon running"; else bad "docker installed but not running — start Docker"; fi
else bad "docker not found"; fi
command -v python3 >/dev/null 2>&1 && ok "python3" || bad "python3 not found (needed by make smoke/oobe)"
command -v curl    >/dev/null 2>&1 && ok "curl"    || bad "curl not found"
command -v nc      >/dev/null 2>&1 && ok "nc (netcat)" || warn "nc not found — readiness waits fall back to a fixed sleep"

section "Host paths (required)"
for v in SOURCE_DIR OUTPUT_DIR TMP_DIR; do
  d="${!v-}"
  if   [ -z "$d" ];   then bad  "$v is not set (copy .env.example -> .env and fill it in)"
  elif [ ! -d "$d" ]; then warn "$v=$d does not exist yet (Docker creates it empty on first mount)"
  elif [ ! -w "$d" ]; then bad  "$v=$d is not writable"
  else ok "$v=$d"; fi
done

section "Server config"
tgt="${DEFAULT_TARGET:-local}"
case "$tgt" in
  local|cloud)            ok "DEFAULT_TARGET=$tgt" ;;
  local-dist|cloud-batch) ok "DEFAULT_TARGET=$tgt (accepted alias for ${tgt%%-*})" ;;
  *)                      bad "DEFAULT_TARGET=$tgt is not a valid target (local | cloud)" ;;
esac
cod="${DEFAULT_CODEC:-both}"
case "$cod" in
  h264|hevc|av1|both|all) ok "DEFAULT_CODEC=$cod" ;;
  *[!a-z0-9,]*|"")        warn "DEFAULT_CODEC=$cod looks off (expected h264|hevc|av1|both|all or a comma list)" ;;
  *)                      ok "DEFAULT_CODEC=$cod (custom subset)" ;;
esac
mc="${MAX_CONCURRENT:-1}"
case "$mc" in ''|*[!0-9]*) warn "MAX_CONCURRENT=$mc is not an integer — the server silently falls back to 1" ;; *) ok "MAX_CONCURRENT=$mc" ;; esac
port="${PORT:-8080}"
if command -v nc >/dev/null 2>&1 && nc -z localhost "$port" 2>/dev/null; then
  warn "server port $port is in use (already running? another app? — check 'make status')"
else ok "server port $port free"; fi

section "Distributed-local (target local-dist)"
for pair in "Temporal:${TEMPORAL_PORT:-7233}" "MinIO:${MINIO_API_PORT:-9000}"; do
  name="${pair%%:*}"; p="${pair##*:}"
  if command -v nc >/dev/null 2>&1 && nc -z localhost "$p" 2>/dev/null; then ok "$name up on :$p"
  else warn "$name not reachable on :$p — run 'make dist-up' before a local-dist encode"; fi
done
if [ -n "${DIST_WORKERS:-}" ]; then
  mip="${MASTER_IP:-192.168.1.10}"
  if [ "$mip" = "192.168.1.10" ]; then
    warn "MASTER_IP=$mip is the placeholder default — set it to THIS master's LAN IP, or remote workers can't reach Temporal/MinIO (they'll start but never poll)"
  else ok "MASTER_IP=$mip"; fi
  for w in $DIST_WORKERS; do
    case "$w" in *=?*) ok "remote worker '$w'" ;; *) bad "DIST_WORKERS entry '$w' is malformed (need label=ssh_target)" ;; esac
  done
else
  ok "DIST_WORKERS empty (master-only local-dist — fine)"
fi

section "Cloud (target cloud-batch)"
[ -n "${STATE_MACHINE_ARN:-}" ] && ok "STATE_MACHINE_ARN set" || warn "STATE_MACHINE_ARN unset — cloud-batch is disabled (run 'make cloud-up' to configure AWS)"
[ -n "${S3_BUCKET:-}" ]         && ok "S3_BUCKET=$S3_BUCKET"   || warn "S3_BUCKET unset — required for cloud-batch staging"
[ -n "${AWS_REGION:-}" ]        && ok "AWS_REGION=$AWS_REGION" || warn "AWS_REGION unset (defaults us-west-2)"
if [ -d "$HOME/.aws" ] || [ -n "${AWS_ACCESS_KEY_ID:-}" ]; then ok "AWS credentials present"; else warn "no ~/.aws and no AWS_ACCESS_KEY_ID — cloud-batch can't authenticate"; fi

section "Result"
if [ "$fails" -gt 0 ]; then
  printf '%s✗ %d problem(s), %d warning(s)%s — fix the ✗ items before running.\n' "$R" "$fails" "$warns" "$N"; exit 1
elif [ "$warns" -gt 0 ]; then
  printf '%s⚠ %d warning(s)%s — advisory; most only matter for one target. Ready for the target whose section is clean.\n' "$Y" "$warns" "$N"; exit 0
else
  printf '%s✓ all checks passed%s\n' "$G" "$N"; exit 0
fi
