# Local cluster: machine performance & concurrency choices

Why the distributed-local encoder runs **4 concurrent HEVC encodes on the Linux
box and 2 on each Mac**, and why we size by *physical performance cores* rather
than the core counts the OS reports. All the numbers below are measured on the
actual machines, not vendor specs.

## The machines

| Box | CPU | Cores as reported | Real compute cores | RAM |
|---|---|---|---|---|
| **ubuntu** | AMD Ryzen 7 5700G | 16 logical | **8 physical** (+ SMT) | 30 GB |
| **MacBook Pro** | Apple Silicon (M-series) | 10 | **4 P** + 6 E | 24 GB |
| **Mac Mini** | Apple Silicon | 10 | **4 P** + 6 E | 16 GB |

The "cores as reported" column is what `nproc` / `os.cpu_count()` returns — and
it's **misleading for encoding** on every one of these boxes, for two different
reasons (SMT on ubuntu, efficiency cores on the Macs).

## Fact 1 — x265 (HEVC) fills cores by *concurrency*, not by threads

x265 does not scale well past a couple of threads per encode (unlike x264). One
HEVC encode only ~half-fills a machine. Measured earlier this session: a single
HEVC encode sat at **~50% CPU**, while running the chunks **in parallel** pushed
the same box to **~1517% of 1600%** (≈15 of 16 logical cores).

**Consequence:** to use a machine fully you run *many encodes at once*, each
pinned to ~2 threads — not one encode with many threads. That's the whole basis
of the chunked-parallel design (and why the cloud path fans chunks out to Batch).

## Fact 2 — an E-core does only ~25% of a P-core for x265

Single-threaded HEVC encode of the same clip, native ffmpeg, pinned to a P-core
(foreground) vs an E-core (`taskpolicy -b` background QoS) on the MacBook Pro:

| Core | Speed | Relative |
|---|---|---|
| **P-core** | 19.5 fps | 1.00× |
| **E-core** | 4.9 fps | **0.25×** |

So a P-core is **4× faster** than an E-core for x265. The Macs' 6 E-cores are
worth only ~1.5 P-cores of encoding between them. Loading them would add a little
throughput at good power-efficiency, **but** in a Docker container we can't steer
work to specific cores anyway (see Fact 4), and the extra complexity/monitoring
noise isn't worth ~25%-each cores. **Decision: use the 4 P-cores, skip the E-cores.**

## Fact 3 — an SMT thread adds only ~25%, and CPU% overstates it

ubuntu is **8 physical cores × 2 SMT threads = 16 logical**. The second thread on
a core shares that core's execution units, so it adds ~25% throughput, not 100%.

Concurrent 360p HEVC encodes on ubuntu (2 threads each), CPU busy % across all 16
logical CPUs:

| Concurrent encodes | CPU busy | Reading |
|---|---|---|
| 1 | 14% | one encode ≈ 2.2 logical cores → **86% idle** |
| 2 | 25% | |
| 4 | 49% | still half idle |
| **8** | **88%** | one thread on every logical CPU → **8 physical cores saturated** |
| 12 | 97% | just packing SMT siblings — little real gain |
| 16 | 99% | SMT oversubscription |

This proves both halves of the intuition: **too few encodes leaves the CPU idle**
(N=1 → 86% idle), and **more concurrency genuinely uses more CPU** — up to the
physical-core ceiling. Past N=8 the % keeps climbing but real throughput barely
moves, because it's just loading SMT siblings. **Decision: size to physical cores
(N≈8/2=4 encodes), not the 16 logical the OS reports.**

> Caveat baked into the tooling: CPU% is measured across *logical* CPUs, so on
> SMT (ubuntu) and E-core (Macs) boxes, "99% busy" ≠ "99% throughput." Any future
> CPU-feedback autoscaler must target well below 95%, or it'll happily
> oversubscribe for near-zero gain.

## Fact 4 — inside Docker we can't see or steer P/E or SMT

Docker Desktop runs a Linux VM that presents **homogeneous vCPUs** — the guest
(and `/proc/stat`) can't tell which are P vs E, and macOS decides which physical
core backs each vCPU. macOS's policy: heavy sustained threads go to **P-cores
first**, spilling to E-cores as load rises. So we can't pin to P-cores from the
container; we can only **feed the right amount of parallel work** and let the OS
place it on P then E.

That's why sizing is done at deploy time from the **host**, not in the container.

## How capacity is decided (the code)

Concurrency = `ENCODE_SLOTS`, set per machine, with `2 threads` per encode
(x265's sweet spot), so `slots × 2 = the performance cores we want busy`:

- **Linux boxes** — the worker reads **physical** cores from `/sys` topology (not
  logical), so ubuntu auto-sizes to `8 / 2 = 4`. SMT is ignored for free.
- **macOS boxes** — the VM hides P/E, so `run-worker.sh` reads the host's P-core
  count (`sysctl hw.perflevel0.physicalcpu = 4`) and sets `ENCODE_SLOTS = 4/2 = 2`.
  Two encodes × 2 threads = 4 threads → the 4 P-cores; E-cores stay idle.
- **RAM guard** — capped at `RAM ÷ 3 GB` as a backstop: a 2-pass **4K** HEVC
  encode peaks at a few GB, and enough concurrent ones would OOM-kill ffmpeg
  (SIGKILL / exit -9). This is what actually caused the one OOM we saw (5
  concurrent 4K on the MacBook Pro's 8 GB VM). With P-core sizing (2 slots) the
  cap rarely binds — 2 × 3 GB fits an 8 GB VM.

## Resulting capacity

| Box | Slots (concurrent HEVC) | Target `docker stats` CPU |
|---|---|---|
| ubuntu | **4** | ~800% (8 physical cores) |
| MacBook Pro | **2** | ~400% (4 P-cores) |
| Mac Mini | **2** | ~400% (4 P-cores) |

In P-core-equivalents for x265 (what actually matters):

| Box | Reported cores | Real x265 capacity |
|---|---|---|
| ubuntu | 16 | ~10 P-equiv (8 physical + SMT) |
| MacBook Pro | 10 | ~5.5 P-equiv |
| Mac Mini | 10 | ~5.5 P-equiv |
| **Cluster** | 36 | **~21 P-equiv**, **8 concurrent HEVC encodes** |

ubuntu is the workhorse (~half the cluster's real muscle); the Macs punch below
their core count because of the weak E-cores.

## Why not just "use every core"?

Because "every core" is a lie told by `nproc`: on ubuntu 8 of the 16 are SMT
siblings worth 25% each; on the Macs 6 of the 10 are E-cores worth 25% each.
Chasing them would push CPU% to 99% while real throughput plateaued, add
scheduling churn, and — for 4K — risk OOM. Sizing to **physical performance
cores** gives ~all the throughput with none of the downside, and it auto-detects
per machine so adding a box (e.g. the Mac Mini) needs no hand-tuning.

## Reproducing the measurements

- **P vs E:** `taskpolicy -b ffmpeg … -c:v libx265 -threads 1 …` (E) vs the same
  without `taskpolicy` (P), compare fps. Needs native ffmpeg + Apple Silicon.
- **Concurrency scaling:** `infra/local-cluster/`-style loop launching N
  `ffmpeg … libx265 -threads 2` jobs and sampling `/proc/stat` busy% (run on the
  all-P ubuntu box so the % isn't muddied by SMT/E).
