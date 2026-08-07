"""AWS unit prices — ONE definition, shared by every module that quotes a cost.

There were three spot rates before this file existed: 0.011 in cli_batch, 0.013
in commercial_cloud, 0.0155 in cli_local. Same quantity, three answers, so a
run's reported cost depended on which code path produced it. All three were also
wrong (#217).

Go keeps its own copies in internal/encode/cost.go because the two languages
cannot share a constant. They MUST be changed together; cost.go says so too.

## Provenance

Re-measure rather than guess. The spot figure below is the 7-day blended mean
across the instance types the compute environment actually launches:

    aws ec2 describe-spot-price-history --region us-west-2 \\
      --instance-types c7g.2xlarge c7g.4xlarge c8g.2xlarge c8g.4xlarge \\
      --product-descriptions "Linux/UNIX" \\
      --start-time <7 days ago>

Measured 2026-08-07, 479 price points:

    c7g.2xlarge   0.0107 - 0.0146 $/vCPU-hr
    c7g.4xlarge   0.0127 - 0.0151
    c8g.2xlarge   0.0118 - 0.0161
    c8g.4xlarge   0.0130 - 0.0161
    blended mean  0.0140          <- AWS_SPOT_VCPU_HR

Spot moves, so this is a running average, not a guarantee: any single run can
land anywhere in that ~0.011-0.016 band. It is an ESTIMATE and is labelled as
one wherever it surfaces.
"""
from __future__ import annotations

# Graviton (c7g/c8g) spot, us-west-2. See the measurement above.
AWS_SPOT_VCPU_HR = 0.014

# Graviton on-demand list price, us-west-2 — the reclaim-proof upper bound we
# quote alongside spot to show what spot avoided. ~2.6x spot.
AWS_ONDEMAND_VCPU_HR = 0.036

# S3 -> internet, us-west-2.
#
# The 100 GB/month free allowance is deliberately NOT modelled. Applying it would
# make the same run quote $0.00 on the 1st and $0.24 on the 20th, so no two runs
# would be comparable and the cheapest-looking run would be whichever happened to
# go first. This is the long-run marginal rate; under the allowance it
# over-states, which is the safe direction for a number whose job is to
# discourage needless transfer.
#
# Mirrors encode.EgressUSDPerGB in internal/encode/cost.go.
EGRESS_USD_PER_GB = 0.09


def egress_usd(num_bytes: int | float) -> float:
    """Dollars for num_bytes leaving S3 for the internet. Flat rate, no free tier."""
    return (num_bytes or 0) / 1e9 * EGRESS_USD_PER_GB


# ---------------------------------------------------------------------------
# Everything else the run touches, priced at FULL RATE.
# ---------------------------------------------------------------------------
#
# The free tier is ignored here for the same reason it is ignored for egress,
# and now consistently: a figure that is zero only because an allowance has not
# run out yet tells you nothing about what the work costs. Half-suppressing it
# would be the worst of both — some lines marginal-rate, others artificially
# zero, and no way to tell which from looking.
#
# The question these answer is "what would this run cost if every free tier were
# already spent?", which is the number that matters when deciding whether to run
# something a hundred more times.
#
# Verified against this account's own bill for 1-3 Aug 2026, where S3 requests
# and storage are already past any allowance and therefore directly checkable:
#
#     Requests-Tier1   591,572  ->  $2.9579   (591.572 x 0.005 = 2.9579)  exact
#     Requests-Tier2   646,011  ->  $0.2584   (646.011 x 0.0004 = 0.2584) exact
#     TimedStorage     5.2 GB-Mo -> $0.1185   (5.2 x 0.023 = 0.1196)      ~1% (tiering)
#
# us-west-2, standard storage class.
S3_TIER1_USD_PER_1K = 0.005      # PUT, COPY, POST, LIST
S3_TIER2_USD_PER_1K = 0.0004     # GET, SELECT
S3_STORAGE_USD_PER_GB_MONTH = 0.023

# Step Functions standard workflows. THIS ACCOUNT IS ALREADY AT 4,000/4,000 free
# transitions, so it bills today — a good example of why modelling only what is
# currently charged would give an answer with a shelf life.
SFN_USD_PER_1K_TRANSITIONS = 0.025

# SQS standard queue (the telemetry channel). 289k/1M free used at time of
# writing, so currently invisible.
SQS_USD_PER_MILLION = 0.40

# CloudWatch Logs. Ingestion dominates; storage is rounding.
CW_LOGS_INGEST_USD_PER_GB = 0.50
CW_LOGS_STORAGE_USD_PER_GB_MONTH = 0.03

HOURS_PER_MONTH = 730.0


def s3_request_usd(tier1: int = 0, tier2: int = 0) -> float:
    return tier1 / 1000 * S3_TIER1_USD_PER_1K + tier2 / 1000 * S3_TIER2_USD_PER_1K


def s3_storage_usd(gb: float, hours: float) -> float:
    """Staging is charged by the GB-hour; a prefix held for a day is not a
    GB-month. Kept explicit because treating it as one over-states by ~30x."""
    return gb * (hours / HOURS_PER_MONTH) * S3_STORAGE_USD_PER_GB_MONTH


def sfn_usd(transitions: int) -> float:
    return transitions / 1000 * SFN_USD_PER_1K_TRANSITIONS


# Named so a reader knows the total is INCOMPLETE and by roughly how much,
# rather than assuming silence means zero. Reported alongside the total; see
# #217 — the whole issue existed because a partial number looked total.
UNMODELLED = (
    "cloudwatch-logs",   # per-run ingest bytes are not tracked; ~$0.16/3 days
    "sqs",               # telemetry message count not threaded through; ~$0.12/3 days
    "ecr-storage",       # image storage, shared across all runs, not per-run
    "data-transfer-in",  # always free
)
