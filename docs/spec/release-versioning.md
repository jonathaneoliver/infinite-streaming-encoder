# Release and versioning: what a version answers, and what moves when

Three registries' worth of tags, two of which are mutable, pointing at code that
runs in four places. This file says which tag means what, which acts move which
consumers, and why a "failed" deploy is the most dangerous outcome rather than
the safest one (#300).

## The core model

**A version here answers *which encoder payload*, not *which build*.**

`IMAGE_TAG` is derived over `Dockerfile`, `requirements.txt`, `scripts/` and
`static/` — the paths a worker actually executes out of the image. `cmd/`,
`internal/` and `go.mod` are excluded. So two boxes agreeing on `IMAGE_TAG` run
identical encoder code even when `HEAD` moved between them, and a Go-only commit
does not move it at all.

| identity | derived from | moves when | who pins it |
| --- | --- | --- | --- |
| `VERSION` | the `VERSION` file | a release is cut | humans |
| `GIT_SHA` | `git rev-parse HEAD` | every commit | humans |
| `IMAGE_TAG` | last commit touching the shipped payload | shipped code changes | Terraform, the AMI bake, `run.json` |

**And publishing is not deploying.** `publish` moves a tag in a registry;
`infra-apply` points Batch job definitions at one. The farm follows a *moving*
tag (`:latest`); the cloud follows a *pinned* one. Neither act moves the other's
consumers, and that separation is what makes a test lane possible at all (#144).

### Why they are separate

Collapsing them was the original design and it had no safe way to test: `:latest`
is public and is what every remote worker pulls via `REMOTE_IMAGE`, so
publishing an unvalidated build handed it to every consumer at once. Splitting
the acts buys a lane — `publish-tag` pushes exactly one immutable tag, the farm
or the cloud can be pointed at it alone, and `:latest` keeps serving the
known-good image until `promote` re-tags the thing that passed.

The cost is that "released" is now three commands and a file to remember the tag
between them, and that the two halves can be left disagreeing — which is what
`cloud-payload` exists to detect rather than prevent.

## The rules that must hold

1. **`IMAGE_TAG` is derived over the payload, not over `HEAD`.** Including
   `cmd/`/`internal/` would re-push ECR, re-bake the AMI and show phantom
   job-def re-tags in `make infra-plan` on every Go commit — and an
   `infra-plan` that is never empty is an `infra-plan` nobody reads.
2. **Two chunks agreeing on `version` ran identical encoder code — and may have
   run under different server binaries, which nothing will ever say.** That
   blindness is by construction (rule 1). Chasing a skew the field declines to
   show is chasing something it was never measuring.
3. **An empty `version` means unknown, never "matches the others".** A machine
   that has not reported is listed in `unknown` rather than counted as agreeing;
   `mixed` is true only when two machines report *different* non-empty versions.
   Claiming a uniform fleet on the strength of boxes that never answered is the
   failure this exists to catch (#248).
4. **The version is durable, not merely live.** `StageProgress.Version` persists
   into every output's `run.json`, because a live fleet warning helps only
   whoever is standing at the terminal — and #248 presented months later, about
   a run nobody was watching.
5. **`publish` moves `:latest`; `publish-tag` moves exactly one tag and touches
   nothing else.** Every tagged push goes through `publish-tag`'s single
   validation choke point, so an invalid tag fails before the build rather than
   deep inside buildx after it.
6. **`promote` RE-TAGS a tested image; it never rebuilds.** A rebuild produces a
   different image from the one that passed, which defeats having tested it
   (#144) — and is not hypothetical here, because the ffmpeg pin is a rolling
   release (see *As it stands*).
7. **`promote` refuses to half-promote.** `publish-tag` skips ECR when cloud is
   unconfigured or `SKIP_ECR=1`, so `FROM` may exist on GHCR alone. Moving GHCR
   `:latest` while the cloud stays pinned to something older is the worst
   available outcome, so that case stops and asks for `GHCR_ONLY=1`.
8. **The moving test alias is GHCR-only, deliberately.** A moving tag in ECR
   would let an already-registered Batch job definition change what it runs with
   no `infra-apply` — collapsing publish and deploy back into one act.
9. **A working-tree build carries `-dirty`.** `farm-test-up` and `cloud-dev-up`
   build the tree, not the commit, so without it a tag claims a commit whose
   contents it does not carry — and two different trees at one `HEAD` collide on
   one tag, where the second silently overwrites the first.
10. **`promote`'s default comes from `.last-published-tag`, not from `DEV_TAG`.**
    You test dirty, then commit: the sha changes and `-dirty` drops, so by
    promote time `DEV_TAG` names a tag that was never pushed.
11. **`require-idle` degrades OPEN and `require-valid-sfn` degrades CLOSED, and
    the inversion is the point.** No AWS creds or no server running means there
    is nothing to protect, and a guard that blocks when it cannot see is worse
    than no guard. A definition that cannot be *validated* is the opposite case:
    Terraform applies job definitions first, so a rejected state machine leaves
    seven definitions bumped and deregistered and the machine pinned to
    revisions that no longer exist — broken, unrolled-back, and re-broken by
    every retry (#283/#284).
12. **`require-idle` runs before EACH disruptive step, not once at entry.** The
    entry check is minutes stale by the time `farm-up` bounces workers or
    `infra-apply` deregisters job definitions; a smoke encode started 29 s after
    a passing entry check and lost its pre-bounce worker's telemetry (#248).
    Each `$(MAKE)` is a fresh sub-process, so the re-check genuinely re-runs
    rather than being skipped as already satisfied.
13. **A failed `deploy` does not mean nothing happened (#300).** The chain stops
    at the first failure, but every step before it has taken effect and two are
    externally visible: `publish` has moved GHCR `:latest`, and `farm-up` has
    restarted the master and every remote box on it. A failure at plan/apply
    therefore leaves the farm on the new payload and the cloud on the old one.
14. **`publish` refuses when a cloud stack exists but this checkout cannot see
    it (#299).** Pushing GHCR would roll the farm onto a payload the cloud is
    not getting, which is rule 13's split arrived at deliberately.
15. **The cloud's payload is read from ACTIVE job definitions matched by IMAGE,
    not by name.** A deploy leaves deregistered revisions behind, and counting
    them would report every tag the stack has ever run; matching by image means
    the check needs no naming convention to stay true.
16. **A rollback by `IMAGE_TAG` restores the encoder payload, not the server
    binary.** Nothing expects it to — the server is not deployed from the pinned
    tag — but "roll back to the previous image" and "roll back to the previous
    build" are not the same sentence.

**Enforced by:** rule 11's closed half by `scripts/check_sfn_definition.py
--require`, which `make check` also runs; rules 2–4 by
`internal/encode/fleet_version_test.go` (which pins the pre-version marker forms
so a regex that stopped matching them cannot take the chunk plot's colouring
with it) and by `FleetVersionSkew`; rules 5, 7, 9 and 10 by `publish-tag`'s tag
validation, `promote`'s raw-manifest digest comparison and the `DEV_DIRTY` /
`LAST_TAG_FILE` derivations. Rules 1, 8, 15 and 16 are **conventions with no
failing test** — nothing breaks at build time when `IMAGE_TAG`'s path list grows
`internal/`. Rules 12–14 are **enforced by refusal and by saying so**: the guard
re-runs and the failure banner names what already moved, but nothing rolls back.

## Blast radius — what does NOT change

Nothing here touches the naming contract. No release act renames an output, and
a payload rollback leaves `OUTPUT_DIR` untouched — see [`outputs.md`](outputs.md).

The two consumers are genuinely independent. The farm pulls GHCR by a moving
tag; the cloud pulls ECR by a tag Terraform pins. A `farm-test-up` cannot move
the cloud (`SKIP_ECR=1`, and the alias is GHCR-only), and an `infra-apply`
cannot move the farm. Every observed split has come from a *partial* act, never
from one consumer leaking into the other.

Retuning the local/cloud phase split does not go through any of this: moving a
phase to the host changes `infra/` not at all — see [`targets.md`](targets.md).

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| publish straight to `:latest` | every consumer gets an unvalidated build at once | one command | superseded (#144) |
| `publish-tag` → test → `promote` | three acts and a file to carry the tag between them | the released image is byte-identical to the tested one | shipped |
| a `:stable` pointer the farm follows | another moving tag to keep honest | testing without touching what consumers pull | named in #144, not built — the tagged lane is the safe path instead |
| a moving alias in ECR too | a job definition could change payload with no apply | one less sha to copy between boxes | rejected |
| `deploy` as one chain | not atomic; a mid-chain failure splits farm and cloud | one command for a whole release | shipped, with `cloud-payload` as the detector rather than a guard |
| pre-baked worker AMI | a bake + wire step, and an AMI that goes stale against `IMAGE_TAG` | cloud workers skip the cold image pull | shipped, opt-in (`USE_AMI=1`), value unmeasured (#162) |

Detection rather than prevention is deliberate throughout the second half of
that table. A half-finished deploy is only one way to a split payload; applying
infra without publishing, a partial apply, or a console edit all produce the
same state with no deploy involved.

## As it stands

Tag sets, as of 2026-08-13. GHCR carries `:latest`, `:$(VERSION)`,
`:$(GIT_SHA)` and `:$(IMAGE_TAG)`; ECR carries `:latest` and `:$(IMAGE_TAG)`
only — the tag Terraform pins by, with `VERSION` and the sha existing for humans
reading the public package. `promote` mirrors those sets exactly, so a promoted
image is indistinguishable from a published one.

Provenance is baked twice over: OCI labels (`image.version` = `VERSION`,
`image.revision` = `GIT_SHA`, `image.ref.name` = `IMAGE_TAG`) and environment
(`ENCODER_IMAGE_TAG`, `ENCODER_GIT_SHA`, `ENCODER_VERSION`). The worker reports
`ENCODER_IMAGE_TAG`, or `"unknown"` when the image predates the stamps — which
is itself the signal that the box is running something old. `imageinfo` reads
labels from GHCR only; the cloud image is an ECR ref it cannot read.

**The image is not reproducible from a commit.** ffmpeg is a pinned *static*
BtbN build but deliberately the rolling `latest` release rather than a dated
autobuild, so two builds of one commit can carry different ffmpeg. That is the
concrete reason rule 6 is a rule and not a preference.

One farm/cloud split is on record: on 2026-08-11 the farm ran `4ab3e12` while
the job definitions ran `459251f`. It became visible only by reading a later
`tofu plan` diff and could otherwise have persisted indefinitely. `cloud-payload`
was written for it.

## What is unmeasured

- **Whether the worker AMI earns its complexity.** Cold-boot cost has never been
  measured against the bake-and-wire step it costs (#162).
- **How often a payload split actually happens.** `make fleet-check` reports both
  halves on demand; nothing counts occurrences, so the one known instance is a
  data point rather than a rate.
- **Whether the rolling ffmpeg pin has ever changed encode output** between two
  builds of the same commit. Nothing compares them, and rule 6 means it normally
  cannot come up — which also means it would not be noticed.
- **Server-binary skew**, which `version` cannot show by construction (rule 2).
  No field answers "which server binary produced this `run.json`".
- **Whether re-running `require-idle` closes the window or merely narrows it.**
  Each `$(MAKE)` boundary is still a gap; #248 proved the entry-only check was
  too wide and nothing has bounded what remains.
- **The cost of deploying at all.** Terminal jobs do not survive a server
  restart, so job history is lost on every deploy (#170) — a recurring price
  with no figure attached.
