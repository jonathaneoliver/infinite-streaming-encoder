# Contributing

Thanks for your interest in contributing to **infinite-streaming-encoder**.

## License and attribution

By contributing, you agree that your contributions are licensed under the terms of the
[`LICENSE`](LICENSE) file in this repository. Attribution to **Jonathan Oliver** must be
preserved in any redistribution.

## How to contribute

1. Fork the repo (public forks are allowed with attribution).
2. Create a feature branch (`feat/<short-description>` or `fix/<short-description>`).
3. Keep changes focused and well-described — one concern per PR.
4. Open a pull request with a concise summary and testing notes.

Direct pushes to `main` are blocked by a git hook — install it once with `make setup-hooks`.
All changes land via PR.

**PR titles use conventional-commit prefixes** (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`,
`refactor:`). Release Drafter autolabels from these and groups the changelog by them — see
[`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md).

## Getting oriented

Before starting:

- Read [`CLAUDE.md`](CLAUDE.md) for the architecture and repo-specific conventions (it's the
  fastest orientation, and is also used by AI coding assistants).
- Read [`docs/PRD.md`](docs/PRD.md) — product behavior source of truth. If you're changing
  user-facing behavior, align with the PRD or update it as part of the PR.
- Skim the design docs under [`docs/`](docs/) for the areas you're touching
  (`chunked-encode-design.md`, `apple-ladder-design.md`).

## Development loop

The whole system is one Docker image playing three roles (server / worker / UI). The farm
(Temporal + MinIO + server + a worker) comes up from one `docker-compose.yml` with profiles.

```bash
cp .env.example .env      # set SOURCE_DIR / OUTPUT_DIR / TMP_DIR
make doctor               # preflight: validates .env, host tools, per-target config
make farm-dev-up          # build from your working tree + bring up the whole master farm
make logs                 # follow the server log
make farm-down            # tear the farm down
```

UI: `http://localhost:8080/`. Drop a file in `SOURCE_DIR` and submit a job.

- **`make farm-dev-up`** builds the image from your working tree and bind-mounts
  `scripts/infinite_streaming_encoder/` into the server + worker, so uncommitted **Python**
  runs everywhere without a rebuild.
- **`make farm-up`** instead pulls the published GHCR image (run `make publish` first).

See the [README](README.md) "three scenarios" table for the local-vs-cloud, working-tree-vs-
published matrix.

### Iterating on the Go control plane

```bash
go build ./cmd/server
go vet ./...
gofmt -l .                # should print nothing
```

The Go code is stdlib-only and shells out to `docker` / `ssh` / `aws` / `python3` — there's
no Docker or Temporal SDK dependency. Most control-plane changes are fastest to reason about
with a full `make farm-dev-up` rebuild (layer-cached).

### Iterating on the encoding pipeline

The real encoder is the Python package under `scripts/infinite_streaming_encoder/` (stdlib
only, except `cloud/*` which uses boto3). With `make farm-dev-up` running, edits there take
effect on the next job with no rebuild (the code is bind-mounted).

### Iterating on the UI

`static/index.html` is a single self-contained vanilla-JS file — no build step. Edit and
reload the page.

## Testing

**There is no automated unit-test suite.** Correctness is verified with a manual smoke matrix
— see [`docs/TESTING.md`](docs/TESTING.md). Before any change touching chunking,
orchestration, packaging, the cloud path, or the farm scripts, run the relevant topologies:

- **`make smoke`** — automated single-box local farm encode, end to end (generates a tiny
  clip, encodes it, asserts the `.m3u8` output). This is also what CI runs on every PR.
- **Two-box / cross-arch / cloud** — the manual tests 2–4 in `docs/TESTING.md` (they need your
  hardware or cost a few cents on AWS).

State in your PR which topologies you ran.

## Code style

- **Go**: standard `gofmt`; run `go vet ./...` before submitting. No extra tooling.
- **Python**: stdlib-first, readable, no framework. Keep `cloud/*` the only boto3 boundary.
- **Shell**: `set -euo pipefail` at the top of new scripts.
- **JavaScript / CSS / HTML**: no build step — `static/index.html` is loaded directly, so
  stick to browser-native ES2020+ features and keep changes explicit and readable.

## Before submitting

- No personal info, secrets, PATs, or per-user LAN IPs / hostnames in the diff.
- If you changed an env var or behavior, update the README / `.env.example` / `docs/`.
- If output-dir naming changed, remember it's a contract shared by `OutputStem`, the encode
  script, `parseOutputMeta`, `resolveCodec`, and the watcher — update all of them (see
  `CLAUDE.md`).
