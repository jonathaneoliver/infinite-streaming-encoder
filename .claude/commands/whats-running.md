---
description: Establish which code is actually running where — farm, cloud, and this tree — before changing anything
allowed-tools: Bash, Read, Grep
---

# whats-running

Answer "is it deployed?" properly. That question was asked ~15 times across this
project's sessions and it is really four questions with four different answers.
Do not guess from git state — every one of these is measured.

## Gather (run these; they are read-only and cheap)

```bash
make status          # deployed vs what this tree says it should be
make fleet-check     # connected workers + payload version; falls through to cloud-payload
curl -s localhost:8080/api/jobs        # is anything non-terminal RIGHT NOW
docker compose -p infinite-streaming-encoder ps --format '{{.Service}} {{.Image}}'
git -C . status --short && git log --oneline -1
git worktree list
```

If the server is unreachable on :8080, say so and stop guessing — `fleet-check`
already prints "cannot tell who is connected" in that case, and neither the
fleet nor the job list can be inferred from anything else.

## Interpret — this is the part that matters

Report these five lines, in this order, and nothing else unless asked:

1. **Job in flight?** — name, id, and progress of any non-terminal job.
   Anything non-terminal means `farm-up` / `farm-dev-up` / `deploy` / `restart`
   is unsafe. A bounce mid-encode does not fail the run: it completes and
   **PASSES**, silently missing the telemetry of every chunk that ran on the
   replaced worker.
2. **Is the farm dev-mounted, and from whose worktree?** — look for
   `docker-compose.dev.yml` in the running project and resolve `HOST_SCRIPTS_DIR`.
   If it points at a worktree that is not this one, say so loudly: that tree
   owns the compose stack, its uncommitted Python is what is executing, and
   **editing a file in it is deploying it** (`cli_phase` is a fresh subprocess
   per phase — no bounce required).
3. **Is the LAN fleet uniform?** — every connected worker on the same
   `version`. A mixed fleet is not an error and does not need fixing; it needs
   *saying*, because its symptom is telemetry that is a subset and reads as
   complete.
4. **Does cloud agree with local?** — `cloud-payload` vs the farm's version. A
   split (farm on X, ACTIVE Batch job definitions on Y) means a local-dist run
   and a cloud run of the same source are different encoders, which is a
   confound no #167/#286-style comparison can absorb.
5. **Server binary vs image tag.** `version` means ENCODER PAYLOAD, not build:
   `IMAGE_TAG` is `git log -1 --format=%h -- Dockerfile requirements.txt scripts static`.
   A Go-only change publishes different image content under the *same* tag.
   **When the binary's gitSha differs from IMAGE_TAG, say "expected — Go-only
   change" rather than flagging skew.** Getting this backwards is the single
   most common way this question is answered wrongly.

## Verdict

End with one sentence the user can act on:

> **Safe to bounce** — nothing running, fleet uniform at 3676141, cloud agrees.

or

> **NOT safe** — job 1786156649420 (insane_fpv, h264) at 214/343, and the farm
> is dev-mounted from `worktrees/rustling-dreaming-crab`.

## Do not

- Do not run `make deploy`, `make farm-up`, or any encode. This command reads.
- Do not "fix" a mixed fleet or a payload split unless asked. Report it.
