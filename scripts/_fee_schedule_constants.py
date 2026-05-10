"""
Hyperliquid post-May-5-2026 fee schedule reference constants (read-only).

Source of truth for live decisions is the `userFees` info endpoint (queried
dynamically by `audit_fee_tier.py`). These constants are a documentation
anchor + a sanity-check cross-comparison: if the live endpoint disagrees
from these values by more than 0.1 bp on any band, `audit_fee_tier.py`
emits a `fee_schedule_drift_warning` insight (git-diff-visible signal that
HL silently updated the schedule).

References (HEAD-checked 200 on 2026-05-10):
- `https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees`
- `https://crypto.news/hyperliquid-unveils-new-fee-discount-system-and-account-linking-feature-for-may-5/`

Filed by tracker `hl-fee-schedule-may5-tier-audit`
(research-20260510-1154.md §1).

Spot fees are stubbed (we run a perps-only grid bot); kept for symmetry +
future use.
"""

# Base perp fees, expressed in basis points, BEFORE HYPE-staking discount.
# (Hyperliquid base tier — 0 HYPE staked, <$5M 14d volume.)
PERP_BASE_MAKER_BP = 1.5   # 0.015%
PERP_BASE_TAKER_BP = 4.5   # 0.045%

# Spot base — not exercised by passivbot today; recorded for completeness.
SPOT_BASE_MAKER_BP = 4.0   # 0.040%
SPOT_BASE_TAKER_BP = 7.0   # 0.070%

# HYPE-staking discount tiers (May 5 2026). discount_pct is fractional
# (0.05 = 5% off); min_hype is the staking threshold for that tier.
HYPE_STAKING_TIERS = [
    {"name": "None",     "min_hype":      0, "discount_pct": 0.00},
    {"name": "Wood",     "min_hype":     10, "discount_pct": 0.05},
    {"name": "Bronze",   "min_hype":    100, "discount_pct": 0.10},
    {"name": "Silver",   "min_hype":   1000, "discount_pct": 0.15},
    {"name": "Gold",     "min_hype":  10000, "discount_pct": 0.20},
    {"name": "Platinum", "min_hype": 100000, "discount_pct": 0.30},
    {"name": "Diamond",  "min_hype": 500000, "discount_pct": 0.40},
]

# HL native staking APR (rough; HL validator yields fluctuate). Configurable
# via --staking-apr CLI flag in audit_fee_tier.py.
HL_NATIVE_STAKING_APR_DEFAULT = 0.03

# Tolerance in basis points for the live-vs-static cross-check. If the
# `userFees` endpoint reports rates that disagree from PERP_BASE_*_BP by
# more than this, audit_fee_tier.py emits a drift warning.
DRIFT_TOLERANCE_BP = 0.1
