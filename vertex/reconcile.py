"""Fill in how past auctions actually closed (paper-trade scoring).

    python -m vertex.reconcile

For every logged auction that has ended, has a median comp, and is not scored
yet, this asks eBay how it ended (ended listings stay readable for a few days).
If eBay no longer has it, it falls back to The Card API, searching with the same
query that comped it and matching on the eBay item number. It then fills:

  actual_close_price  the final price, blank if the auction did not sell
  actual_bid_count    bids on it, when reported
  would_have_won      "no"       closed at or above our max bid (a tie goes to the earlier bidder)
                      "possibly" closed below our max bid, or ended with no bids at an opening
                                 price we would have met. Only "possibly", because a hidden
                                 max above ours could still have won.
                      blank      we had no max bid for it
  profit_at_close     for "possibly" rows: resale net minus the closing (or opening) price
                      minus shipping, i.e. the best case if we had won
  close_source        "ebay", "ebay_unsold", "card_api", or "not_found" once neither
                      source has it after --give-up-days
"""

import argparse
import csv
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .buy_rule import sell_side_net
from .cardapi import CardApiClient, CardApiError, CardApiQuotaExceeded, SalesQuery
from .ebay import EbayClient, EbayError, Outcome, parse_time
from .output import PREDICTION_COLUMNS

log = logging.getLogger("vertex.reconcile")


def _item_number(row: dict) -> str | None:
    """eBay item number of a sold row, from its URL (…/itm/<n> or …/itm/<slug>/<n>) or id."""
    match = re.search(r"/itm/(?:[^/?#]+/)?(\d+)", str(row.get("listing_url") or ""))
    if match:
        return match.group(1)
    match = re.search(r"(\d{9,})", str(row.get("id") or ""))
    return match.group(1) if match else None


def card_api_close(cards: CardApiClient, prediction: dict, max_rows: int) -> dict | None:
    end = parse_time(prediction["auction_end_utc"]).date()
    query = SalesQuery(q=prediction["comp_query"], date_from=end, date_to=end + timedelta(days=1), platform="ebay")
    for row in cards.sales(query, max_rows):
        if _item_number(row) == prediction["item_id"]:
            return row
    return None


def _net(prediction: dict) -> float:
    return sell_side_net(float(prediction["median_comp"]))


def score_sold(prediction: dict, close: float, bid_count: int | None, source: str) -> None:
    prediction["actual_close_price"] = f"{close:.2f}"
    prediction["actual_bid_count"] = "" if bid_count is None else str(bid_count)
    prediction["close_source"] = source
    if not prediction["max_bid"]:
        return
    if close >= float(prediction["max_bid"]):
        prediction["would_have_won"] = "no"
        return
    prediction["would_have_won"] = "possibly"
    shipping = float(prediction["inbound_shipping"] or 0)
    prediction["profit_at_close"] = f"{_net(prediction) - close - shipping:.2f}"


def score_unsold(prediction: dict, outcome: Outcome) -> None:
    prediction["actual_bid_count"] = "" if outcome.bid_count is None else str(outcome.bid_count)
    prediction["close_source"] = "ebay_unsold"
    if not prediction["max_bid"] or prediction["bids_at_scan"] != "0" or not prediction["price_at_scan"]:
        return
    opening = float(prediction["price_at_scan"])
    if float(prediction["max_bid"]) >= opening:
        # Nobody bid, so a bid at our max would have won at the opening price.
        prediction["would_have_won"] = "possibly"
        shipping = float(prediction["inbound_shipping"] or 0)
        prediction["profit_at_close"] = f"{_net(prediction) - opening - shipping:.2f}"


def _parse_price(sale: dict) -> float:
    return float(sale.get("price") if sale.get("price") is not None else sale.get("sale_price"))


def reconcile(path: Path, ebay: EbayClient, cards: CardApiClient | None, now: datetime,
              settle: timedelta = timedelta(minutes=30), card_api_lag: timedelta = timedelta(hours=36),
              give_up: timedelta = timedelta(days=7), max_lookups: int = 1500,
              max_rows: int = 300) -> dict[str, int]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != PREDICTION_COLUMNS:
            raise SystemExit(f"{path} columns do not match this version of Vertex")
        predictions = list(reader)

    counts = {"scored": 0, "unsold": 0, "not_found": 0, "pending": 0}
    # The same auction is often logged by several runs; look it up once, score every copy.
    results: dict[str, tuple[str, object]] = {}
    lookups = 0
    try:
        for p in predictions:
            if p["close_source"] or not p["median_comp"]:
                continue
            age = now - parse_time(p["auction_end_utc"])
            if age < settle:
                counts["pending"] += 1
                continue
            item = p["item_id"]
            if item not in results:
                if lookups >= max_lookups:
                    counts["pending"] += 1
                    continue
                lookups += 1
                results[item] = _look_up(p, ebay, cards, age, card_api_lag, max_rows)
            kind, value = results[item]
            if kind == "sold":
                close, bids, source = value
                score_sold(p, close, bids, source)
                counts["scored"] += 1
            elif kind == "unsold":
                score_unsold(p, value)
                counts["unsold"] += 1
            elif age >= give_up:
                p["close_source"] = "not_found"
                counts["not_found"] += 1
            else:
                counts["pending"] += 1
    except CardApiQuotaExceeded as exc:
        log.warning("%s; saving what was scored so far", exc)
    finally:
        _rewrite(path, predictions)
    return counts


def _look_up(p: dict, ebay: EbayClient, cards: CardApiClient | None, age: timedelta,
             card_api_lag: timedelta, max_rows: int) -> tuple[str, object]:
    outcome = ebay.outcome(f"v1|{p['item_id']}|0")
    if outcome.status == "sold" and outcome.price is not None:
        return "sold", (outcome.price, outcome.bid_count, "ebay")
    if outcome.status == "unsold":
        return "unsold", outcome
    if cards is None or age < card_api_lag or not p["comp_query"]:
        return "pending", None
    sale = card_api_close(cards, p, max_rows)
    if sale is None:
        return "pending", None
    return "sold", (_parse_price(sale), sale.get("bids"), "card_api")


def _rewrite(path: Path, predictions: list[dict]) -> None:
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as handle:
        writer = csv.DictWriter(handle, fieldnames=PREDICTION_COLUMNS)
        writer.writeheader()
        writer.writerows(predictions)
    Path(handle.name).replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vertex.reconcile", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", type=Path, default=Path("logs/predictions.csv"))
    parser.add_argument("--give-up-days", type=float, default=7)
    parser.add_argument("--max-lookups", type=int, default=1500,
                        help="cap on eBay getItem calls (Browse allows 5,000/day; default 1500)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    env = {name: os.environ.get(name, "").strip() for name in ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "CARD_API_KEY")}
    if not (env["EBAY_CLIENT_ID"] and env["EBAY_CLIENT_SECRET"]):
        raise SystemExit("Missing environment variable(s): EBAY_CLIENT_ID and EBAY_CLIENT_SECRET")
    if not args.log.exists():
        raise SystemExit(f"No prediction log at {args.log}; run `python -m vertex` first")
    ebay = EbayClient(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"])
    cards = CardApiClient(env["CARD_API_KEY"]) if env["CARD_API_KEY"] else None
    try:
        counts = reconcile(args.log, ebay, cards, datetime.now(timezone.utc),
                           give_up=timedelta(days=args.give_up_days), max_lookups=args.max_lookups)
    except (EbayError, CardApiError) as exc:
        log.error("%s", exc)
        return 1
    log.info("Scored %(scored)s sold, %(unsold)s unsold, %(not_found)s given up, %(pending)s still pending.",
             counts)
    log.info("eBay calls: %s. Card API rows used: %s.", ebay.calls, cards.rows_used if cards else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
