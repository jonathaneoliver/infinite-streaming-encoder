<!--
Thanks for the PR. A couple of conventions for this repo:

- Use a conventional-commit prefix in the title: feat:, fix:, docs:, chore:, ci:, refactor:.
  Release Drafter autolabels from these and groups the changelog by them. Add a `breaking`
  label by hand if your change is a breaking change.

- Direct pushes to main are blocked (git hook — `make setup-hooks`). Everything lands via PR.
-->

## Summary
<!-- 1–3 bullets on what this PR does and why. -->

## Why
<!-- The user-visible problem or use case this addresses. Skip if obvious from the title. -->

## Test plan
<!-- There's no unit-test suite — verify with the smoke matrix in docs/TESTING.md. -->

- [ ] `make smoke` passes (single-box local farm encode, end to end).
- [ ] Ran the relevant topology from `docs/TESTING.md` for what I changed
      (two-box / cross-arch / cloud), or it's not applicable.
- [ ] `go vet ./...` and `gofmt -l .` are clean (for Go changes).
- [ ] Updated docs (README / CLAUDE.md / .env.example / docs/) if behaviour or env changed.
- [ ] No personal info, secrets, or per-user LAN IPs / hostnames in the diff.

## Screenshots
<!-- For UI changes — drag-and-drop a before/after. Skip otherwise. -->
