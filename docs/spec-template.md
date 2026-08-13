# Spec template

The skeleton for a behavioural spec in this repo, and the reasoning behind what
it leaves out. Copy the block below into `docs/<slug>.md` and delete this
preamble.

Derived by auditing the specs already here: `chunked-encode-design.md` was
opened once after it was written, `apple-ladder-design.md` and `PRD.md` zero
times. Every section that encoded a **plan** was stale within a week — the
orchestration option those docs recommend is not the one that shipped, the
`concat` step one of them designs was removed 71 minutes later, and
"Decision: 30s chunks" is now dynamic sizing with a 12s floor. Every section
that encoded an **invariant, a measurement, or a blast radius** is still
load-bearing.

`ladders-and-delivery.md` is the one written this way, and the only spec
CLAUDE.md links to. It is the worked example.

## What goes in the ISSUE instead

Not in the doc, deliberately — these are consumed once, by the implementer,
during the session that follows:

- touch-point tables (file × change)
- sequencing / phased rollout
- data-model code blocks
- a recommendation between options
- acceptance criteria
- a `Status:` header — the issue carries status; both existing design docs
  still say "Status: proposed" and both shipped over a month ago

---

```markdown
# <Object or behaviour>: <the one-line claim>

<2-3 sentences: why this document exists. Name the defect that made it
necessary, with an issue number. If nothing broke, say what would break
without the rules below.>

## The core model

<The ONE distinction everything else depends on — the thing that is not
obvious and that a reader will get wrong. Usually two things that look like
one thing, or one thing that looks like two. A small table if it has fields.>

### Why it is one object / why they are separate

<The alternative you rejected and the cost of the choice you made. This is
what stops the next person re-litigating it, and it is the section most often
needed and not found.>

## The rules that must hold

1. **<Rule>** — <what breaks when it doesn't, concretely.>
2. …

**Enforced by:** <test / check / guard>, or explicitly **"Not enforced."**
An unenforced rule is a rule; an unenforced rule that reads as enforced is a
trap. Say which.

## Blast radius — what does NOT change

<What keeps working, and why. Name the contracts that stay intact — OutputStem,
resolveCodec, parseOutputMeta, the watcher's alreadyEncoded, the job-definition
Ref:: set — and the ones that don't. This section ages best of all of them.>

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |

<A status column, always. It is what let one such table survive while the
section above it rotted.>

## As it stands

<Current measured state — a table of real values, dated. Where the numbers came
from and, critically, what they DON'T cover: "all of this is one 4K AV1
high-motion clip; if a different master doesn't reproduce, that is the first
thing to suspect rather than a regression.">

## What is unmeasured

<Only measurements, never decisions. "How much does gop=1 cost vs gop=6 —
unmeasured, and it bounds the value of the whole idea (#288)." A decision
belongs in the issue; a missing measurement belongs here and stays useful until
someone takes it.>
```

---

If the spec states a rule that is needed at **edit** time — a contract between
two files, a naming convention, an ordering that fails silently — put the
one-line version in CLAUDE.md's "Things to know when editing" and link here.
CLAUDE.md is auto-loaded into every session; `docs/` is not, and that difference
is the whole reason this template exists.
