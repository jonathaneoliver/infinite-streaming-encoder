---
description: Sweep every worktree for work that should go to main, and propose a landing order
allowed-tools: Bash, Read, Grep
---

# merge-sweep

"What do I have here that needs to go to main?" was asked 14 times across this
project's sessions, and each session invented its own procedure. This is the
procedure.

## 1. Enumerate

```bash
git worktree list
git fetch origin
```

For **each** worktree, from inside it (never `cd` out of a worktree-isolated
session — run these with `git -C <path>`):

```bash
git -C <path> status --short
git -C <path> log --oneline origin/main..HEAD
git -C <path> log --oneline HEAD..origin/main | wc -l     # how far behind
```

Then, repo-wide:

```bash
gh pr list --state open --json number,title,headRefName,isDraft,statusCheckRollup
git branch -r --merged origin/main | grep -v 'origin/main$'
```

## 2. Report per tree

| tree | branch | dirty | ahead | behind | PR | checks |

Plus two explicit callouts:

- **Untracked files that must never be committed** — `ENCODER_COMPARISON.md`,
  anything matching `.env*` except `.env.example`, `*.playwright.png`,
  `fleet-lanes.png`. Name them so they are consciously left behind rather than
  swept in.
- **Uncommitted work in a tree that is currently the live `HOST_SCRIPTS_DIR`
  mount.** That tree's Python is executing right now. Landing or rebasing it
  mid-encode changes the code of a job already in flight. Check with
  `/whats-running` first if any job is non-terminal.

## 3. Propose an order, do not execute

Rank by: green PRs first, then clean trees ahead of main with no PR, then dirty
trees. Say what each one closes. Stop and let the user pick — they say "merge N"
or "do them all".

## 4. Rules that have already cost this project something

- **`Closes #N` goes in the PR *body*, one line per issue.** A `fix(#N):` commit
  subject closes nothing. Multiple issues per PR need multiple keywords.
- **Stacked PRs: retarget the child BEFORE merging the parent.** Merging the
  parent with `--delete-branch` auto-closes the child, which then cannot be
  retargeted or reopened.
- **Never push or fast-forward `main` directly** — the pre-push hook blocks it
  and everything lands via PR.
- **Never bare `git stash` / `git stash pop`.** The stash stack is shared across
  all worktrees and other sessions may be using it. Prefer a temporary WIP
  commit; if you must stash, `git stash push -u -m "<unique-tag>"`, capture the
  SHA from `git stash list --format='%H %gs'`, restore with `git stash apply
  <sha>`, and drop it by re-finding the tag.
- **`make check` before proposing a merge**, and read its skips: `staticcheck`,
  `govulncheck`, `tofu` and `ruff` skip silently when not installed, and `sfn
  schema` skips permanently in CI. A green summary line is not the same as a
  green gate.
- **`make smoke` before merging anything touching the chunk/dispatch contract;
  `make smoke-cloud` when the state machine or job definitions change.** Ask the
  user to run them — do not launch encodes.

## 5. Branch cleanup

Only after a merge lands, and only branches already merged into `origin/main`.
List them and ask before deleting.
