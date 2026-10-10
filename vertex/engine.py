"""Tonight's run: watchlist -> ending auctions -> comps -> buy rule -> evaluations.

Paper trade only. Nothing here bids, and nothing talks to a sniper.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from . import buy_rule
from .card_identity import has_player, identity_from_listing, junk_reason
from .cardapi import CardApiClient, CardApiQuotaExceeded
from .comp_stats import passes_gate, summarize
from .comps import CompResult, fetch_comps
from .config import BUY_RULE, COMP_RULE
from .ebay import EbayClient, ListingDetails
from .models import Evaluation, Listing
from .watchlist import WatchlistRow

log = logging.getLogger(__name__)

# No listing can ever be bid above this, whatever its comps say.
HIGHEST_POSSIBLE_ALL_IN = buy_rule.floor_cents(buy_rule.max_all_in_cost(BUY_RULE.max_median))


class ShortCompWindow(RuntimeError):
    """The Card API plan returns less history than the buy rule's 30-day lookback."""


@dataclass
class RunOptions:
    run_at: datetime
    window_hours: float
    comp_platform: str | None
    max_rows_per_card: int
    max_item_lookups: int = 2500
    allow_short_comp_window: bool = False


@dataclass
class RunResult:
    evaluations: list[Evaluation]
    window_end: datetime
    comp_rows_used: int
    comp_coverage_from: str | None


def _prefilter(e: Evaluation) -> str | None:
    """Cheap checks that rule a listing out before spending eBay or Card API calls on it."""
    listing, row = e.listing, e.row
    word = row.excluded_by(listing.title)
    if word:
        return f"title contains excluded word '{word}'"
    junk = junk_reason(listing.title)
    if junk:
        return junk
    if not has_player(listing.title, row.player):
        return f"title does not name {row.player}"
    if listing.current_price is None:
        return "no current price"
    if listing.currency != "USD":
        return f"priced in {listing.currency}"
    return _out_of_reach(listing)


def _out_of_reach(listing: Listing) -> str | None:
    """True when no median in the value band could produce a max bid at or above the next bid."""
    next_bid = buy_rule.minimum_next_bid(listing.current_price, listing.bid_count)
    ceiling = HIGHEST_POSSIBLE_ALL_IN - (listing.shipping_cost or 0.0)
    if next_bid > ceiling:
        return f"next bid ${next_bid:.2f} already above the highest possible max bid ${max(ceiling, 0):.2f}"
    return None


def scan(rows: list[WatchlistRow], ebay: EbayClient, options: RunOptions) -> tuple[list[Evaluation], datetime]:
    window_end = options.run_at + timedelta(hours=options.window_hours)
    evaluations: list[Evaluation] = []
    seen: set[str] = set()
    for row in rows:
        listings = ebay.search_auctions(row.search_query, row.category_id, options.run_at, window_end)
        fresh = [l for l in listings if l.item_id not in seen and options.run_at < l.end_time <= window_end]
        seen.update(l.item_id for l in fresh)
        log.info("%s: %d auctions ending by %s", row.label, len(fresh), window_end.isoformat())
        evaluations.extend(Evaluation(row=row, listing=l) for l in fresh)
    evaluations.sort(key=lambda e: e.listing.end_time)
    return evaluations, window_end


def run(rows: list[WatchlistRow], ebay: EbayClient, cards: CardApiClient, options: RunOptions) -> RunResult:
    evaluations, window_end = scan(rows, ebay, options)
    cutoff = (options.run_at - timedelta(days=COMP_RULE.lookback_days)).date()
    cache: dict[tuple, CompResult] = {}
    lookups = 0
    quota_hit = False

    # Soonest-ending first, so any budget that runs out cuts the latest auctions.
    for e in evaluations:
        e.no_bid_reason = _prefilter(e)
        if e.no_bid_reason:
            continue

        if lookups >= options.max_item_lookups:
            e.no_bid_reason = "eBay item-lookup budget for this run used up"
            continue
        lookups += 1
        if not _identify(e, ebay.get_details(e.listing.item_id)):
            continue

        if quota_hit:
            e.no_bid_reason = "Card API daily row allowance used up before this card was comped"
            continue
        key = e.identity.key()
        if key not in cache:
            try:
                cache[key] = fetch_comps(cards, e.identity, cutoff, options.comp_platform, options.max_rows_per_card)
            except CardApiQuotaExceeded as exc:
                log.warning("%s", exc)
                quota_hit = True
                e.no_bid_reason = "Card API daily row allowance used up before this card was comped"
                continue
            _check_coverage(cards, cutoff, options)
        _apply_comps(e, cache[key])

    return RunResult(
        evaluations=evaluations,
        window_end=window_end,
        comp_rows_used=cards.rows_used,
        comp_coverage_from=cards.coverage_from.isoformat() if cards.coverage_from else None,
    )


def _identify(e: Evaluation, details: ListingDetails | None) -> bool:
    """Attach item specifics and work out exactly which card this is. False = no bid."""
    if details is None:
        e.no_bid_reason = "eBay item details unavailable"
        return False
    shipping = details.shipping_cost if details.shipping_cost is not None else e.listing.shipping_cost
    e.listing = replace(e.listing, aspects=details.aspects, shipping_cost=shipping,
                        condition_id=details.condition_id or e.listing.condition_id)
    if shipping is None:
        e.no_bid_reason = "auction shipping is calculated or unknown"
        return False
    e.no_bid_reason = _out_of_reach(e.listing)
    if e.no_bid_reason:
        return False
    e.identity, reason = identity_from_listing(
        e.listing.title, e.row.player or None, e.listing.aspects, e.listing.condition_id)
    if e.identity is None:
        e.no_bid_reason = reason
        return False
    if not e.identity.set_name:
        e.no_bid_reason = "listing has no Set item specific, so comps cannot be matched safely"
        return False
    return True


def _check_coverage(cards: CardApiClient, cutoff, options: RunOptions) -> None:
    if cards.coverage_from is None or options.allow_short_comp_window:
        return
    if cards.coverage_from > cutoff + timedelta(days=1):
        days = (options.run_at.date() - cards.coverage_from).days
        raise ShortCompWindow(
            f"The Card API is only returning sales back to {cards.coverage_from} (~{days} days). "
            f"The buy rule needs a {COMP_RULE.lookback_days}-day median, which takes the Builder plan "
            "or higher. Re-run with --allow-short-comp-window to paper-trade on the shorter window anyway."
        )


def _apply_comps(e: Evaluation, comps: CompResult) -> None:
    e.comp_query = comps.query
    e.comps_returned = comps.returned
    e.comps_matched = comps.matched
    e.comp_rejections = comps.rejections
    e.summary = summarize([c.price for c in comps.matched])
    if not passes_gate(e.summary):
        e.no_bid_reason = (
            f"only {e.summary.count} matching sold comps after trimming "
            f"(need {COMP_RULE.min_comps})"
        )
        return
    e.decision = buy_rule.evaluate(e.summary.median, e.listing.shipping_cost)
    if not e.decision.is_bid:
        return
    e.min_next_bid = buy_rule.minimum_next_bid(e.listing.current_price, e.listing.bid_count)
    if e.min_next_bid > e.decision.max_bid:
        e.no_bid_reason = (
            f"next bid ${e.min_next_bid:.2f} already above max bid ${e.decision.max_bid:.2f}"
        )
