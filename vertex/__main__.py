"""Vertex Phase 0 nightly job (paper trade).

    python -m vertex                      # scan the next 12 hours
    python -m vertex --hours 8 --tz America/New_York

Reads watchlist.csv, writes bid_list.csv, and appends to logs/. Places no bids.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import output
from .cardapi import CardApiClient, CardApiError
from .config import DEFAULT_COMP_PLATFORM, DEFAULT_WINDOW_HOURS
from .ebay import EbayClient, EbayError
from .engine import RunOptions, ShortCompWindow, run
from .watchlist import load_watchlist

log = logging.getLogger("vertex")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m vertex", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watchlist", type=Path, default=Path("watchlist.csv"))
    parser.add_argument("--out", type=Path, default=Path("bid_list.csv"), help="bid list to (over)write")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--hours", type=float, default=DEFAULT_WINDOW_HOURS,
                        help=f"scan auctions ending within this many hours (default {DEFAULT_WINDOW_HOURS})")
    parser.add_argument("--tz", help="show auction end times in this time zone, e.g. America/New_York "
                                     "(default UTC)")
    parser.add_argument("--comp-platform", default=DEFAULT_COMP_PLATFORM,
                        help="Card API platform for comps; 'all' for every marketplace (default ebay)")
    parser.add_argument("--max-rows-per-card", type=int, default=300,
                        help="cap on Card API rows fetched per distinct card (default 300)")
    parser.add_argument("--max-item-lookups", type=int, default=2500,
                        help="cap on eBay getItem calls per run; Browse allows 5,000 calls/day (default 2500)")
    parser.add_argument("--allow-short-comp-window", action="store_true",
                        help="keep going when your Card API plan returns less than 30 days of sales")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def _require_env(*names: str) -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(f"Missing environment variable(s): {', '.join(missing)}")
    return values


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    tz = None
    if args.tz:
        try:
            tz = ZoneInfo(args.tz)
        except ZoneInfoNotFoundError:
            raise SystemExit(f"Unknown time zone: {args.tz}")

    env = _require_env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "CARD_API_KEY")
    rows = load_watchlist(args.watchlist)
    run_at = datetime.now(timezone.utc).replace(microsecond=0)
    options = RunOptions(
        run_at=run_at,
        window_hours=args.hours,
        comp_platform=None if args.comp_platform.lower() == "all" else args.comp_platform,
        max_rows_per_card=args.max_rows_per_card,
        max_item_lookups=args.max_item_lookups,
        allow_short_comp_window=args.allow_short_comp_window,
    )
    ebay = EbayClient(env["EBAY_CLIENT_ID"], env["EBAY_CLIENT_SECRET"])
    cards = CardApiClient(env["CARD_API_KEY"])

    try:
        result = run(rows, ebay, cards, options)
    except ShortCompWindow as exc:
        log.error("%s", exc)
        return 2
    except (EbayError, CardApiError) as exc:
        log.error("%s", exc)
        return 1

    run_info = output.RunInfo(
        run_id=run_at.strftime("%Y%m%dT%H%M%SZ"),
        run_at=run_at,
        window_end=result.window_end,
        watchlist_rows=len(rows),
        comp_rows_used=result.comp_rows_used,
        comp_coverage_from=result.comp_coverage_from,
    )
    bids = output.write_bid_list(args.out, result.evaluations, tz)
    output.append_predictions(args.log_dir / "predictions.csv", run_info, result.evaluations)
    output.write_comp_audit(args.log_dir / "comps" / f"{run_info.run_id}.jsonl", result.evaluations)
    summary = output.append_run_summary(args.log_dir / "runs.csv", run_info, result.evaluations)

    log.info(
        "Scanned %s auctions, %s comped, %s passed the comp gate, %s bids. "
        "Max-bid exposure $%s, predicted profit if every bid wins at max $%s. "
        "Card API rows used: %s. eBay calls: %s.",
        summary["listings_scanned"], summary["listings_comped"], summary["listings_passing_gate"],
        bids, summary["total_max_bid_exposure"], summary["total_predicted_profit"],
        result.comp_rows_used, ebay.calls,
    )
    log.info("Bid list: %s (PAPER TRADE: nothing was bid)", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
