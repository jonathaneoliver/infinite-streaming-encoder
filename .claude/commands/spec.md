---
description: Write or audit a behavioural spec in docs/ — durable half in the doc, plan half in the issue
argument-hint: <issue-number> | <path/to/existing-spec.md> | --help
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
---

# spec

Write the half of a design document that survives, and put the other half where
it belongs.

## Why this command exists

Measured over this repo's whole history: `docs/chunked-encode-design.md` was
opened **once** after it was written, `docs/apple-ladder-design.md` **zero**
times, `docs/PRD.md` **zero** times. Both design docs were written and
implemented inside a single session and never revisited. Every section that
encoded a *plan* was stale within a week — the orchestration recommendation
(option A, Batch array jobs) is still in the doc while the code shipped option
B; the `concat` step it designs was removed 71 minutes after the doc mentioned
it; "Decision: 30s chunks" became dynamic-with-a-12s-floor and then a 2-minute
reclaim target.

Every section that encoded an *invariant, a measurement, or a blast radius* is
still load-bearing today.

The failure showed up plainly on 2026-08-10, when the user needed a past
decision back and searched **session history**, not `docs/`.

## Resolve the argument FIRST — do this before anything else

There is no flag parser here; you are it. Classify the argument, and when it is
not unambiguously one of the two modes, **print usage and stop**. Do not guess:
an unrecognised argument falling through to Mode B means trying to audit a file
named `--help`, which is how this section came to exist.

| argument | do |
| --- | --- |
| empty, `--help`, `-h`, `help`, `?` | print **Usage** below, stop |
| all digits (`312`) | **Mode A** |
| digits with a leading `#` (`#312`) | strip the `#`, **Mode A** |
| a path that EXISTS | **Mode B** |
| a path that does NOT exist | say so, `ls docs/*.md`, stop |
| anything else | say it is ambiguous, show Usage, stop |

Check existence before choosing Mode B — `test -f <arg>` — and resolve relative
to the repo root, not the cwd.

### Usage

```
/spec <issue-number>     write a new spec from that issue      e.g. /spec 312
/spec <path-to-spec.md>  audit an existing spec against code   e.g. /spec docs/PRD.md
/spec --help             this message
```

Then list what is actually available to audit: `ls docs/*.md`, excluding
`spec-template.md` (that is the skeleton, not a spec) and noting that
`ladders-and-delivery.md` is the reference example rather than an audit target.

## Mode A — new spec (argument is an issue number)

1. `gh issue view <N> --json number,title,body,comments` — read the issue and
   any decisions in its comments.
2. Write `docs/<slug>.md` from the skeleton in `docs/spec-template.md` (see
   "The skeleton" below — read that file, do not recall it).
3. **Write the plan half into the ISSUE, not the doc** — touch-point tables
   (file × change), sequencing/phasing, acceptance criteria, and any
   option-recommendation. Post it as an issue comment or fold it into the body.
4. Link the issue from the doc's opening paragraph. **Never write a
   `Status: proposed` header** — both existing design docs still carry one and
   both shipped over a month ago. The issue carries status; the doc does not.

## Mode B — audit (argument is an existing spec path)

For each numbered rule and each invariant in the doc, find the code that holds
it and report one of: **holds** (cite `file:line`), **holds but unenforced**,
or **contradicted** (cite what the code does instead). Do the same for every
code block and every table. Report; do not silently rewrite.

Flag `Status:` headers, touch-point tables and sequencing sections for deletion
— they are the sections with a measured zero-percent survival rate here.

## The skeleton

**It lives in `docs/spec-template.md`. Read that file and use the fenced block
inside it — do not reproduce a skeleton from memory and do not copy one into
this command.**

Two files that must agree is the failure mode this repo writes rules about, and
an earlier version of this command embedded its own copy of the template. There
is one source now.

If `docs/spec-template.md` is missing, say so and stop rather than inventing a
structure. A spec written to a remembered shape is exactly the artefact the
template exists to prevent.

## Reference

`docs/ladders-and-delivery.md` is the one spec in this repo written this way,
and the only one CLAUDE.md links to. Read it before writing a new one.

## Finally

If the spec states a rule CLAUDE.md needs at edit time — a contract between two
files, a naming convention, an ordering that fails silently — add the one-line
version to CLAUDE.md's "Things to know when editing" and link the doc. CLAUDE.md
is auto-loaded; `docs/` is not, and that difference is the whole finding above.
