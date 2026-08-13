---
description: Write or audit a behavioural spec in docs/ — durable half in the doc, plan half in the issue
argument-hint: <issue-number> | <path/to/existing-spec.md>
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

## Mode A — new spec (argument is an issue number)

1. `gh issue view <N> --json number,title,body,comments` — read the issue and
   any decisions in its comments.
2. Write `docs/<slug>.md` from the skeleton below.
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

```markdown
# <Object or behaviour>: <the one-line claim>

<2-3 sentences: why this document exists. Name the defect that made it
necessary, with an issue number. If nothing broke, say what would break
without the rules below.>

## The core model

<The ONE distinction everything else depends on — the thing that is not
obvious and that a reader will get wrong. Usually two things that look like
one thing, or one thing that looks like two.>

### Why it is one object / why they are separate

<The alternative you rejected and the cost of the choice you made. This is
what stops the next person re-litigating it.>

## The rules that must hold

1. **<Rule>** — <what breaks when it doesn't, concretely.>

**Enforced by:** <test / check / guard>, or explicitly **"Not enforced."**
An unenforced rule is a rule; an unenforced rule that reads as enforced is a
trap.

## Blast radius — what does NOT change

<What keeps working and why. Name the contracts that stay intact — OutputStem,
resolveCodec, parseOutputMeta, the watcher, the job-definition Ref:: set — and
the ones that don't.>

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |

<A status column, always. It is what let one table survive while the section
above it rotted.>

## As it stands

<Current measured state — real values, dated, with where they came from and
what they DON'T cover. e.g. "all of this is one 4K AV1 high-motion clip; if a
different master doesn't reproduce, suspect the source before suspecting a
regression.">

## What is unmeasured

<Only measurements, never decisions. "How much does gop=1 cost vs gop=6 —
unmeasured, and it bounds the value of the whole idea (#288)." A decision
belongs in the issue; a missing measurement belongs here and stays useful
until someone takes it.>
```

## Reference

`docs/ladders-and-delivery.md` is the one spec in this repo written this way,
and the only one CLAUDE.md links to. Read it before writing a new one.

## Finally

If the spec states a rule CLAUDE.md needs at edit time — a contract between two
files, a naming convention, an ordering that fails silently — add the one-line
version to CLAUDE.md's "Things to know when editing" and link the doc. CLAUDE.md
is auto-loaded; `docs/` is not, and that difference is the whole finding above.
