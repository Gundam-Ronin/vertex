from dataclasses import dataclass, field
from datetime import datetime

from .buy_rule import BidDecision
from .card_identity import CardIdentity, CompRecord
from .comp_stats import CompSummary
from .watchlist import WatchlistRow


@dataclass(frozen=True)
class Listing:
    """An eBay auction ending tonight, as returned by the Browse API."""

    item_id: str
    legacy_item_id: str | None
    title: str
    url: str
    end_time: datetime
    current_price: float | None
    currency: str
    bid_count: int
    shipping_cost: float | None  # None: calculated, missing, or not shipped to the US
    condition_id: str | None = None
    aspects: dict[str, str] = field(default_factory=dict)


@dataclass
class Evaluation:
    """Everything decided about one listing in one run, for the bid list and the log."""

    row: WatchlistRow
    listing: Listing
    identity: CardIdentity | None = None
    comp_query: str | None = None
    comps_returned: int = 0
    comps_matched: list[CompRecord] = field(default_factory=list)
    comp_rejections: dict[str, int] = field(default_factory=dict)
    summary: CompSummary | None = None
    decision: BidDecision | None = None
    min_next_bid: float | None = None
    no_bid_reason: str | None = None

    @property
    def is_bid(self) -> bool:
        return self.no_bid_reason is None and self.decision is not None and self.decision.is_bid
