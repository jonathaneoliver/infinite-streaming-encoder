# Release notes

Hand-written notes, one file per release, tracked here because **the GitHub
draft is not a safe place to keep them**.

`.github/workflows/release-drafter.yml` runs on every push to `main`, finds the
existing draft, and overwrites its body — so notes written into the draft
survive exactly until the next merge. That is not a bug in the workflow; the
drafter owns that draft by design. It means the durable copy has to live
somewhere else, and here it also gets reviewed in the PR that changes it.

Restore a clobbered draft from its file:

```bash
gh release edit v0.2.0 --tag v0.2.0 --title v0.2.0 \
  --notes-file docs/release-notes/v0.2.0.md
```

## Two things the drafter cannot know

**The version.** `./VERSION` is the single source of truth — the Makefile reads
it, stamps it into the Go binary via ldflags, and tags the GHCR image with it.
Release Drafter resolves its own version from PR labels instead, starting at
`0.0.1`, so it proposes a tag unrelated to what is actually published. Check the
draft's tag against `cat VERSION` before publishing.

**The baseline.** With no *published* release, the drafter has nothing to
compare against and emits "No changes" regardless of how many PRs merged. It
says so in the draft body. Publishing one release fixes this permanently:
afterwards each draft accumulates the PRs merged since, in the categories set by
`.github/release-drafter.yml`, and a hand-written file like these is only needed
when a release deserves more than a list of PR titles.
