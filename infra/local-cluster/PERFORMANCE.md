# Local cluster: machine performance & concurrency choices

Why the distributed-local encoder runs **4 concurrent HEVC encodes on the Linux
box and 2 on each Mac**, and why we size by *physical performance cores* rather
than the core counts the OS reports. All numbers below are **measured on the
actual machines** (HEVC, `libx265 -preset medium`), not vendor specs.

## The machines

| Box | CPU | Cores as reported | Real compute cores | RAM |
|---|---|---|---|---|
| **MacBook Pro** | Apple **M5** | 10 | **4 P** + 6 E | 24 GB |
| **Mac Mini** | Apple **M4** | 10 | **4 P** + 6 E | 16 GB |
| **ubuntu** | AMD Ryzen 7 5700G | 16 logical | **8 physical** (+ SMT) | 30 GB |

"Cores as reported" (`nproc` / `os.cpu_count()`) is **misleading for encoding** on
every box — SMT inflates ubuntu, efficiency cores inflate the Macs.

## Measurement pitfalls (learned the hard way)

Two things will lie to you if you're not careful — both bit us:

1. **Compare the same ffmpeg version.** Our first cross-machine test used each
   box's *native* ffmpeg — Mac 8.0.1 vs ubuntu 6.1.1 — and showed the Mac 2×
   faster. Level the x265 version (same container image) and the real hardware
   gap is only ~1.35×. **Most of that "2×" was the newer x265, not the chip.**
2. **Measure where the work runs — in the container.** All encoding runs inside
   the Docker image, and that costs **~30% vs native** (VM overhead on the Macs +
   the image's older/differently-built libx265). Native-vs-container mixing skews
   any comparison.

So every number below is **in-container, same image family** unless noted.

## Fact 1 — x265 fills cores by *concurrency*, not by threads

x265 doesn't scale well past a couple of threads per encode (unlike x264): one
HEVC encode only ~half-fills a machine (measured: single HEVC ~50% CPU, chunked
in parallel ~1517% of 1600%). **So you use a machine by running many encodes at
once, each on ~2 threads — not one encode with many threads.** That's the basis
of the chunked-parallel design.

## Fact 2 — within a Mac, an E-core does ~25% of a P-core

Single-thread HEVC of the same clip on the MacBook, pinned to a P-core
(foreground) vs an E-core (`taskpolicy -b`):

| Core | Speed | Relative |
|---|---|---|
| P-core | 19.5 fps | 1.00× |
| E-core | 4.9 fps | **0.25×** |

A P-core is **4× faster** than an E-core for x265; the 6 E-cores are worth ~1.5
P-cores between them. **Decision: use the 4 P-cores, skip the E-cores** — and in
a container we can't steer to specific cores anyway (Fact 5).

## Fact 3 — per-core, across machines (the honest comparison)

Single-thread 1080p HEVC, **in-container, matched ffmpeg (~7.1.x)** — one core,
no threading or version confounds:

| Chip | fps/core | vs ubuntu core |
|---|---|---|
| **M5** (MacBook) | 18.5 | 1.35× |
| **M4** (Mac Mini) | 18.4 | 1.35× |
| **Ryzen 5700G** (ubuntu) | 13.7 | 1.00× |

- **M4 = M5** — identical for encoding (18.4 vs 18.5). The Mac Mini is a true equal.
- **A Mac P-core is ~1.35× an ubuntu core** — not 2×. And this *understates* the
  Macs: ubuntu is native Docker (no VM tax) while the Macs pay the Docker-Desktop
  VM tax, so the raw M-core lead is even bigger.

## Fact 4 — an SMT thread adds only ~25%, and CPU% overstates it

ubuntu is **8 physical × 2 SMT = 16 logical**. The second thread on a core shares
its execution units → ~25% more, not 100%. Concurrent 360p HEVC (2 threads each),
CPU busy % across all 16 logical CPUs:

| Concurrent | CPU busy | Reading |
|---|---|---|
| 1 | 14% | one encode ≈ 2.2 logical cores → **86% idle** |
| 4 | 49% | still half idle |
| **8** | **88%** | one thread per logical CPU → **8 physical cores saturated** |
| 12 | 97% | just packing SMT siblings |
| 16 | 99% | SMT oversubscription |

Proves both halves: **too few = idle** (N=1 → 86% idle), **more = more** up to the
physical ceiling; past N=8 it's SMT with little real gain. **Decision: size to
physical cores (8/2 = 4 encodes), not 16 logical.**

> Baked-in caveat: CPU% is over *logical* CPUs, so on SMT (ubuntu) and E-core
> (Macs) boxes "99% busy" ≠ "99% throughput." A future CPU-feedback autoscaler
> must target well below 95% or it'll oversubscribe for near-zero gain.

## Fact 5 — inside Docker we can't see or steer P/E or SMT (and it costs ~30%)

Docker Desktop's Linux VM presents **homogeneous vCPUs** — the guest can't tell P
from E; macOS places heavy threads on P-cores first, spilling to E as load rises.
So we can't pin from the container; we only feed the right amount of parallel
work. And the container itself costs **~30%** vs native (M5: native 48.6 fps →
container 34.0 fps, 2-thread) from VM overhead + the image's libx265. Sizing is
therefore done at deploy time from the **host**, not in the container.

## How capacity is decided (the code)

Concurrency = `ENCODE_SLOTS`, `2 threads` per encode (x265's sweet spot), so
`slots × 2 = the performance cores we want busy`:

- **Linux** — the worker reads **physical** cores from `/sys` topology, so ubuntu
  auto-sizes to `8/2 = 4`; SMT ignored for free.
- **macOS** — the VM hides P/E, so `run-worker.sh` reads the host's P-core count
  (`sysctl hw.perflevel0.physicalcpu = 4`) and sets `ENCODE_SLOTS = 4/2 = 2`;
  the 4 encode threads land on the 4 P-cores, E-cores idle.
- **RAM guard** — capped at `RAM ÷ 3 GB`: a 2-pass **4K** HEVC encode peaks at a
  few GB, and enough concurrent ones OOM-kill ffmpeg (exit -9). This caused the
  one OOM we saw (5 concurrent 4K on the MacBook's 8 GB VM). With P-core sizing
  (2 slots) the cap rarely binds.

## Resulting capacity

| Box | Slots (concurrent HEVC) | Target `docker stats` CPU |
|---|---|---|
| ubuntu | **4** | ~800% (8 physical cores) |
| MacBook Pro (M5) | **2** | ~400% (4 P-cores) |
| Mac Mini (M4) | **2** | ~400% (4 P-cores) |

Weighting each box's encoding cores by **measured per-core speed** (Fact 3):

| Box | cores × fps/core | Contribution |
|---|---|---|
| ubuntu | 8 × 13.7 | **~110** (~43%) |
| MacBook Pro | 4 P × 18.5 | ~74 |
| Mac Mini | 4 P × 18.5 | ~74 |

So ubuntu is the **biggest single contributor (~1.5× a Mac)** — but not the 2× a
naive core-count or the confounded native test suggested, and not "equal thirds."
The Macs punch well above their P-core count because each P-core is faster.

## Why not just "use every core"?

`nproc` lies: on ubuntu 8 of 16 are SMT siblings worth ~25% each; on the Macs 6
of 10 are E-cores worth ~25% each. Chasing them pushes CPU% to 99% while real
throughput plateaus, adds scheduling churn, and — for 4K — risks OOM. Sizing to
**physical performance cores** gives ~all the throughput with none of the
downside, and auto-detects per machine, so adding a box (the Mac Mini) needed no
hand-tuning.

## Open opportunity

The ~30% container-vs-native gap on the Macs (Fact 5) is real throughput left on
the table — a **newer libx265 in the encoder image** would speed up *every*
encode on *every* box for free. Worth a spike.

## Reproducing the measurements

- **P vs E (within a Mac):** `taskpolicy -b ffmpeg … -c:v libx265 -threads 1 …`
  (E) vs the same without (P); needs native ffmpeg on Apple Silicon.
- **Per-core across machines:** run `libx265 -threads 1 -x265-params pools=1`
  **in the same container image** on each box (`-benchmark`, fps = frames/rtime).
- **Concurrency scaling:** launch N `libx265 -threads 2` jobs, sample `/proc/stat`
  busy% — do it on the all-P ubuntu box so the % isn't muddied by SMT/E.
