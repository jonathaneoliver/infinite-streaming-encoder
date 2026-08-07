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
