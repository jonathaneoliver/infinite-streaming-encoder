# Cost: the estimate, the bill, and the one basis they must share

Two numbers describe the same run — what the app quoted before you pressed
Encode, and what it reported afterwards. They are computed by different code
from different inputs, and the only thing that makes them comparable is a shared
definition of what is being paid for.

## The core model

**AWS bills the INSTANCE, not the vCPU-time jobs allocate.** The rental starts
at launch and ends at termination: boot, image pull, queue idle and the
scale-down tail are all charged, and none of them appear in a job's allocated
vCPU-seconds.

So the two numbers measure different things by nature:

| number | measures | computed by |
| --- | --- | --- |
| the estimate | predicted **allocation** | `projectCloudCost` |
| the reported cost | actual **rental** | `_emit_cost_summary` |

and one term reconciles them:

```
machine = allocated / (1 - idle)
```

with `idle` from `fleetIdleFraction`, learned from past runs.

### Why the allowance is shown and never folded in

Before that term existed the same app quoted ~60% of what it then reported
(#237). The fix could have been to silently multiply the estimate by ~1.7 — and
that is precisely how the gap survived as long as it did. An invisible
correction next to the Encode button is indistinguishable from an accurate
model. Displaying the allowance makes the assumption falsifiable by the operator
looking at it.

## The rules that must hold

1. **The estimate and the reported cost stay on ONE basis.** Any change to
   either that alters what is being priced must change both.
2. **The idle allowance is SHOWN, never folded in.**
3. **`variantResourcesFor`'s RESERVATION stays on both sides.**
   `unallocated_pct` is defined against *reserved* vCPU, so pricing measured
   busy-cores instead would break the calibration and restore the undercount.
   The reservation is a packing weight, not a hard cap — Batch uses CPU shares.
4. **Cost figures never subtract free tier.** Every figure is what the run would
   cost with the allowance already exhausted, because the free tier is a
   property of the account's month rather than of the run. Folding it in makes
   two identical runs quote different numbers, and makes a `$0.00` line read as
   "this is free" when it means "you have not hit the cap yet".
5. **`projectCloudCost` hardcodes `graviton` on purpose.** One compute env,
   c8g/c7g only, so a cloud job cannot land on Intel or AMD. Honouring
   `JobConfig.CPUArch` would quote hardware the run can never reach — which is
   why the form's cpu-arch control is hidden as retired legacy.
6. **Egress is priced at `EgressUSDPerGB` with no free-tier discount**, so the
   saving from `skip_media_download` is the same figure on any day of the month.
7. **With host packaging on, the bytes billed as egress are the CHUNKS pulled**,
   not the packaged output the sync-back no longer fetches. `cli_batch` recovers
   that number by scanning `cli_phase`'s own printed fetch measurement as it
   relays it — a regex against a print. Reword either and the run reports zero
   egress, making host packaging look like a saving rather than the trade it is.
8. **`$STATE_DIR/spot_samples.json` is a cross-language contract read by field
   name.** Adding fields is safe; renaming one silently zeroes the AWS view's
   spot savings with no error on either side.
9. **`idle_pct` there is a LOWER BOUND** — boxes still alive at run end have
   their lifetime measured to now — and samples whose spans overlap another run
   are excluded from the allowance, because a concurrent run's time on a shared
   instance counts as this run's idle.
10. **Local hardware cost is reported as ENCODE TIME, not $0.** Slowness is its
    only downside and pricing it at zero hides the comparison the operator is
    actually making.

**Enforced by:** rule 3 by `idle_allowance_test.go` and `cost_marker_test.go`;
rule 7 by `scripts/test_host_package.py`, which pins the regex to the print.
Rules 1, 2, 4 and 10 are **conventions with no failing test** — nothing breaks
when a new figure is added on the wrong basis.

## Blast radius — what does NOT change

Nothing here affects encode behaviour. Every figure is derived after the fact
or predicted before it; no cost model input changes what ffmpeg is asked to do.

The comparison line items (MediaConvert, commercial, AWS on-demand, AWS spot,
local hardware) are presentation over the same underlying model — adding or
removing one does not touch the basis above.

## The trade

| option | what it costs | what it buys | status |
| --- | --- | --- | --- |
| price allocation only | undercounts by ~40% | simple, needs no learned state | rejected (#237) |
| price rental, correct the estimate by learned idle | the allowance is a moving target and is a lower bound | the two numbers describe the same thing | shipped |
| fold the allowance into the quote | — | nothing; it is how the gap hid | rejected |

## As it stands

Measured on a real cloud run and written up in the README: **egress is 64% of
the cost, compute 28%** — which is not obvious and is the reason
`skip_media_download` and deferred packaging exist at all.

Measured over 23 days on the real account: **S3 egress tracks Tier1 (LIST)
request count at r=0.99 and spot hours at only r=0.33** — it is nearly
independent of whether anything is encoding. On a day with zero encodes that was
62,865 LIST requests and 20.3 GB out, ~$2/day and ~79% of the whole bill, for a
table usually not on screen. That finding is why the S3 staging walk is opt-in
per request (`?s3=1`) and never on a timer.

`null` s3_prefixes means **not measured** — never cache it as "nothing staged",
and never render it as `0 B`.

## What is unmeasured

- **Whether the learned idle fraction is stable across fleet sizes.** It is one
  number, and a 3-instance run and a 12-instance run may not share it.
- **The commercial comparison figures.** Mux was dropped from the line items
  because the cost model was not trusted; the remaining third-party rates are
  external claims, not measurements.
- **Cost of the local target beyond wall time.** Electricity, wear and the
  opportunity cost of a saturated laptop are not modelled and probably should
  not be, but "local is free" is not quite true either.
