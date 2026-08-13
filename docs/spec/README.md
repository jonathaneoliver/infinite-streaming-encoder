# Behavioural spec

What this system **does**, observably, given inputs. It complements
[`../PRD.md`](../PRD.md), which says what the product is *for* and what it must
achieve; this says what actually happens when you press the button.

Everything here describes **shipped behaviour on `main`**, derived from the
implementation rather than from intent, and is meant to be falsifiable: if the
code and a claim here disagree, the claim is a bug. Designed-but-unshipped
behaviour is deliberately absent — each file ends with what is *unmeasured*, and
open issues are cited by number without their intended behaviour being described
as if it existed.

## The files

| file | covers |
| --- | --- |
| [`ingest.md`](ingest.md) | how a file becomes a job — watcher, upload, API, CLI — and the skip/narrow rules |
| [`job-lifecycle.md`](job-lifecycle.md) | the five job states, what moves between them, and cancel / resume / redo |
| [`chunk-plan.md`](chunk-plan.md) | how a clip is divided: dynamic / fixed / whole, segment snapping, the duration limit, the whole-job budget |
| [`outputs.md`](outputs.md) | output directory naming, the four states an output can be in, and what each offers |
| [`targets.md`](targets.md) | `local` vs `cloud` — what genuinely differs, and what deliberately does not |
| [`cost.md`](cost.md) | the estimate, the reported cost, and the one basis they must share |
| [`retention.md`](retention.md) | what is reclaimed, when, and what is structurally exempt |
| [`release-versioning.md`](release-versioning.md) | what a version answers, which act moves which consumer, and why a failed deploy is the dangerous outcome |

The ninth domain — **ladders and delivery profiles** — already has a spec in
this shape: [`../ladders-and-delivery.md`](../ladders-and-delivery.md). It is
not duplicated here. It is also the reference example for
[`../spec-template.md`](../spec-template.md).

## What this set does NOT cover

Stated so the list above is not mistaken for the system. Each of these is
observable behaviour with no spec — several are documented in `README.md` or in
module comments, but none in this falsifiable shape:

- **the operator interface** (`static/index.html`) — the chunk grid, the machine
  timeline, the Ladders and AWS tabs. Every file here defers *to* the page;
  [`outputs.md`](outputs.md) rule 3 even parks an unenforced ordering rule there
- **progress and telemetry as a contract** — the `[[ENCODER-*]]` marker classes
  and what each transport may drop (CLAUDE.md has the edit-time half)
- **quality and the VMAF curves** — seeded from one extreme-motion clip, feeding
  rung-redundancy analysis and the burn-in's estimated VMAF row
- **scheduling and fairness** — `localJobRank`, Batch job priority,
  `MAX_CONCURRENT`, `ENCODE_SLOTS`; failures here are invisible in output
- **the farm as a machine pool** — worker death and Temporal rescheduling
- **the AWS estate beyond staging** — inventory, the `MaxLifetime` instance reaper
- **media transforms the operator can see** — the always-AAC-stereo audio rule,
  padding math, burn-in layers
- **the trust boundary** — `SECURITY.md` states the LAN-trust posture; nothing
  specifies what is validated where

## How these relate

A submission flows through them roughly in file order:

```
source file ──ingest──► JobConfig ──chunk-plan──► per-variant chunk grid
                            │                            │
                            └────────targets─────────────┘
                                     (local farm | cloud Batch)
                                          │
                                     job-lifecycle
                                          │
                                       outputs ──► retention
                                          │
                                        cost
```

Two cross-cutting rules live in the files that own them rather than being
repeated:

- **Naming is a contract** across `OutputStem`, the encode scripts, `resolveCodec`,
  `parseOutputMeta` and the watcher. It is specified once, in
  [`outputs.md`](outputs.md), and every other file defers to it.
- **The two targets run the same encoder.** Differences are enumerated in
  [`targets.md`](targets.md); anywhere else, assume identical behaviour, because
  that is the property the whole design is protecting.

## What this spec is not

- **Not an API reference.** Route shapes change more often than behaviour does;
  the routes are listed in [`ingest.md`](ingest.md) and [`outputs.md`](outputs.md)
  only where the behaviour is the point.
- **Not an internals map.** How the code is organised, and which contracts fail
  silently when edited, is [`../../CLAUDE.md`](../../CLAUDE.md). That file is
  auto-loaded into every session; this one is not, which is why anything needed
  *at edit time* belongs there and not here.
- **Not a roadmap.** See the open issues.

## Maintaining it

Audit a file against the code with `/spec docs/spec/<file>.md`. It reports each
rule as **holds** (with `file:line`), **holds but unenforced**, or
**contradicted**.

The failure mode to watch for is the one that killed the earlier design docs:
sections that encode a *plan* rather than an invariant. If a section here starts
listing files to change or steps to take, it has drifted into the issue's job.
