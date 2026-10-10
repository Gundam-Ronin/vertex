"""The buy rule (SPEC.md Section 3, with the owner's 2026-10-10 decisions).

    sell-side net  = median - (13.25% x median + $0.40) - $4.00 outbound shipping
    max all-in     = min(sell-side net - 10% x median,  70% x median)
    max bid        = max all-in - the auction's shipping charge to us

"All-in" is what we pay to acquire the card: hammer price plus inbound shipping.
Predicted profit is the profit if we win at exactly our max bid. That is the
worst case, because a lower close only raises it.
"""

import math
from dataclasses import dataclass

from .config import BUY_RULE, BuyRule


@dataclass(frozen=True)
class BidDecision:
    max_all_in: float | None
    max_bid: float | None
    predicted_profit: float | None
    no_bid_reason: str | None

    @property
    def is_bid(self) -> bool:
        return self.no_bid_reason is None


def floor_cents(amount: float) -> float:
    # The epsilon absorbs float noise such as 10.95 being stored as 10.949999...
    return math.floor(amount * 100 + 1e-6) / 100


def sell_side_net(median: float, rule: BuyRule = BUY_RULE) -> float:
    """What we keep from reselling at the median comp, after eBay fees and postage."""
    fees = median * rule.final_value_fee_rate + rule.order_fee
    return median - fees - rule.outbound_shipping


def max_all_in_cost(median: float, rule: BuyRule = BUY_RULE) -> float:
    formula = sell_side_net(median, rule) - median * rule.target_margin_rate
    return min(formula, median * rule.ceiling_rate)


def evaluate(median: float, inbound_shipping: float | None, rule: BuyRule = BUY_RULE) -> BidDecision:
    if not rule.min_median <= median <= rule.max_median:
        return BidDecision(
            None, None, None,
            f"median comp ${median:.2f} outside ${rule.min_median:.0f}-${rule.max_median:.0f} band",
        )
    max_all_in = floor_cents(max_all_in_cost(median, rule))
    if inbound_shipping is None:
        return BidDecision(max_all_in, None, None, "auction shipping is calculated or unknown")
    max_bid = floor_cents(max_all_in - inbound_shipping)
    if max_bid <= 0:
        return BidDecision(
            max_all_in, None, None,
            f"shipping ${inbound_shipping:.2f} leaves no room under the ${max_all_in:.2f} ceiling",
        )
    predicted_profit = round(sell_side_net(median, rule) - max_bid - inbound_shipping, 2)
    return BidDecision(max_all_in, max_bid, predicted_profit, None)


# eBay US bid increments: (price floor, increment), highest floor first.
_BID_INCREMENTS = (
    (5000.00, 100.00),
    (2500.00, 50.00),
    (1000.00, 25.00),
    (500.00, 10.00),
    (250.00, 5.00),
    (100.00, 2.50),
    (25.00, 1.00),
    (5.00, 0.50),
    (1.00, 0.25),
    (0.00, 0.05),
)


def bid_increment(price: float) -> float:
    for floor, increment in _BID_INCREMENTS:
        if price >= floor:
            return increment
    return _BID_INCREMENTS[-1][1]


def minimum_next_bid(current_price: float, bid_count: int) -> float:
    """The lowest bid eBay would accept right now.

    With no bids yet, current price is the opening price and is itself a valid bid.
    """
    if bid_count <= 0:
        return current_price
    return round(current_price + bid_increment(current_price), 2)
