"""Write tonight's bid list and append to the paper-trade logs.

bid_list.csv          Overwritten each run. Only listings that pass the buy rule.
logs/predictions.csv  Appended each run. One row per listing evaluated (bid or not),
                      with blank actual_* columns to fill in after the auctions close.
logs/runs.csv         Appended each run. One summary row: counts and predicted P&L.
logs/comps/<run>.jsonl  The exact comps behind every median, for auditing matches.
"""

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Evaluation

BID_LIST_COLUMNS = (
    "card_title", "item_url", "auction_end_time", "max_bid",
    "comp_count", "median_comp", "predicted_profit",
)

PREDICTION_COLUMNS = (
    "run_id", "run_at_utc", "watchlist", "item_id", "card_title", "item_url",
    "auction_end_utc", "card_identity", "comp_query", "comps_returned", "comps_matched",
    "comp_count", "median_comp", "max_all_in", "inbound_shipping", "max_bid",
    "price_at_scan", "bids_at_scan", "predicted_profit", "decision", "reason",
    # Filled in later, after the auctions close.
    "actual_close_price", "actual_bid_count", "would_have_won", "profit_at_close", "close_source",
)

RUN_COLUMNS = (
    "run_id", "run_at_utc", "window_end_utc", "watchlist_rows", "listings_scanned",
    "listings_comped", "listings_passing_gate", "bids", "total_max_bid_exposure",
    "total_predicted_profit", "comp_rows_used", "comp_coverage_from",
)


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    run_at: datetime
    window_end: datetime
    watchlist_rows: int
    comp_rows_used: int
    comp_coverage_from: str | None = None


def _money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item_number(listing) -> str:
    """eBay's 12-digit item number, which is what the item URL and sold records use."""
    if listing.legacy_item_id:
        return listing.legacy_item_id
    parts = listing.item_id.split("|")
    return parts[1] if len(parts) == 3 else listing.item_id


def _display_time(value: datetime, tz: ZoneInfo | None) -> str:
    if tz is None:
        return _utc(value)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def write_bid_list(path: Path, evaluations: list[Evaluation], tz: ZoneInfo | None = None) -> int:
    bids = sorted((e for e in evaluations if e.is_bid), key=lambda e: e.listing.end_time)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(BID_LIST_COLUMNS)
        for e in bids:
            writer.writerow((
                e.listing.title,
                e.listing.url,
                _display_time(e.listing.end_time, tz),
                _money(e.decision.max_bid),
                e.summary.count,
                _money(e.summary.median),
                _money(e.decision.predicted_profit),
            ))
    return len(bids)


def _append_rows(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    if not is_new:
        with path.open(newline="", encoding="utf-8") as handle:
            existing = next(csv.reader(handle), [])
        if tuple(existing) != columns:
            raise RuntimeError(
                f"{path} has different columns than this version writes; "
                "move it aside so a fresh log can start"
            )
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)


def prediction_row(run: RunInfo, e: Evaluation) -> dict:
    listing, decision, summary = e.listing, e.decision, e.summary
    return {
        "run_id": run.run_id,
        "run_at_utc": _utc(run.run_at),
        "watchlist": e.row.label,
        "item_id": item_number(listing),
        "card_title": listing.title,
        "item_url": listing.url,
        "auction_end_utc": _utc(listing.end_time),
        "card_identity": e.identity.describe() if e.identity else "",
        "comp_query": e.comp_query or "",
        "comps_returned": e.comps_returned if e.comp_query else "",
        "comps_matched": len(e.comps_matched) if e.comp_query else "",
        "comp_count": summary.count if summary else "",
        "median_comp": _money(summary.median if summary else None),
        "max_all_in": _money(decision.max_all_in if decision else None),
        "inbound_shipping": _money(listing.shipping_cost),
        "max_bid": _money(decision.max_bid if decision else None),
        "price_at_scan": _money(listing.current_price),
        "bids_at_scan": listing.bid_count,
        "predicted_profit": _money(decision.predicted_profit if decision else None),
        "decision": "BID" if e.is_bid else "NO_BID",
        "reason": "" if e.is_bid else (e.no_bid_reason or (decision.no_bid_reason if decision else "")),
    }


def append_predictions(path: Path, run: RunInfo, evaluations: list[Evaluation]) -> None:
    _append_rows(path, PREDICTION_COLUMNS, [prediction_row(run, e) for e in evaluations])


def append_run_summary(path: Path, run: RunInfo, evaluations: list[Evaluation]) -> dict:
    bids = [e for e in evaluations if e.is_bid]
    summary = {
        "run_id": run.run_id,
        "run_at_utc": _utc(run.run_at),
        "window_end_utc": _utc(run.window_end),
        "watchlist_rows": run.watchlist_rows,
        "listings_scanned": len(evaluations),
        "listings_comped": sum(1 for e in evaluations if e.summary is not None),
        "listings_passing_gate": sum(1 for e in evaluations if e.decision is not None),
        "bids": len(bids),
        "total_max_bid_exposure": _money(sum(e.decision.max_bid for e in bids)),
        "total_predicted_profit": _money(sum(e.decision.predicted_profit for e in bids)),
        "comp_rows_used": run.comp_rows_used,
        "comp_coverage_from": run.comp_coverage_from or "",
    }
    _append_rows(path, RUN_COLUMNS, [summary])
    return summary


def write_comp_audit(path: Path, evaluations: list[Evaluation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for e in evaluations:
            if e.comp_query is None:
                continue
            record = {
                "item_id": item_number(e.listing),
                "title": e.listing.title,
                "identity": e.identity.describe() if e.identity else None,
                "comp_query": e.comp_query,
                "comps_returned": e.comps_returned,
                "rejected_by_reason": e.comp_rejections,
                "lower_fence": e.summary.lower_fence if e.summary else None,
                "upper_fence": e.summary.upper_fence if e.summary else None,
                "kept_prices": list(e.summary.kept) if e.summary else [],
                "matched_comps": [
                    {"title": c.title, "price": c.price, "sold_at": c.sold_at,
                     "listing_type": c.listing_type, "url": c.listing_url}
                    for c in e.comps_matched
                ],
            }
            handle.write(json.dumps(record) + "\n")
