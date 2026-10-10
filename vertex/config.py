"""Every number that drives the buy rule, in one place.

The buy-rule constants (fees, shipping, margin, cap, band, comp gate) come from
SPEC.md Sections 3 and 11.4 plus the owner's decisions on 2026-10-10:

* Sell-side fees are computed exactly: 13.25% final value fee + $0.40 order fee.
* Outbound shipping is a fixed $4.00 per card (so the multiplier flexes with value).
* Target margin is 10% of median comp.
* The all-in ceiling is capped at 70% of median comp.
* The auction's own shipping charge to us is subtracted from the max bid;
  calculated or unknown shipping means no bid.
* Median comp must sit in the $20-$80 value band.
* Outliers are trimmed with Tukey 1.5x IQR fences, and at least 8 comps must
  remain after trimming (Section 11.4 liquidity gate).

Change these only deliberately: they are the engine's safety rails.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BuyRule:
    final_value_fee_rate: float = 0.1325
    order_fee: float = 0.40
    outbound_shipping: float = 4.00
    target_margin_rate: float = 0.10
    ceiling_rate: float = 0.70
    min_median: float = 20.00
    max_median: float = 80.00


@dataclass(frozen=True)
class CompRule:
    lookback_days: int = 30
    min_comps: int = 8
    iqr_multiplier: float = 1.5


BUY_RULE = BuyRule()
COMP_RULE = CompRule()

# eBay category "Sports Trading Card Singles". Override per watchlist row.
DEFAULT_CATEGORY_ID = "261328"

# Default length of "tonight": auctions ending between now and now + this many hours.
DEFAULT_WINDOW_HOURS = 12

# Comps are restricted to this Card API platform by default because the resale
# and fee model are eBay's. Pass --comp-platform all to use every marketplace.
DEFAULT_COMP_PLATFORM = "ebay"
