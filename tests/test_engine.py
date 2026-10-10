import csv
import itertools
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from vertex import output
from vertex.cardapi import CardApiClient, CardApiQuotaExceeded
from vertex.ebay import ListingDetails
from vertex.engine import RunOptions, ShortCompWindow, run
from vertex.models import Listing
from vertex.watchlist import WatchlistRow

RUN_AT = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc)
_sale_ids = itertools.count(1)
WEMBY = WatchlistRow("Victor Wembanyama", "Prizm", (), "261328")
PSA10_ASPECTS = {
    "Graded": "Yes",
    "Professional Grader": "Professional Sports Authenticator (PSA)",
    "Grade": "10",
    "Card Number": "136",
    "Season": "2023-24",
    "Set": "2023-24 Panini Prizm",
}


def listing(item_id, title, price=20.0, bids=3, shipping=4.99, hours=2):
    return Listing(
        item_id=f"v1|{item_id}|0",
        legacy_item_id=str(item_id),
        title=title,
        url=f"https://www.ebay.com/itm/{item_id}",
        end_time=RUN_AT + timedelta(hours=hours),
        current_price=price,
        currency="USD",
        bid_count=bids,
        shipping_cost=shipping,
    )


def sale(price, title="2023-24 Panini Prizm Victor Wembanyama #136 RC PSA 10 GEM MINT", days_ago=3, **extra):
    row = {
        "id": f"ebay-{next(_sale_ids)}",
        "platform": "eBay",
        "title": title,
        "price": price,
        "currency": "USD",
        "price_confirmed": True,
        "sale_date": (RUN_AT - timedelta(days=days_ago)).date().isoformat(),
        "listing_type": "auction",
        "listing_url": "https://www.ebay.com/itm/1",
    }
    row.update(extra)
    return row


class FakeEbay:
    def __init__(self, listings, details):
        self.listings, self.details, self.calls = listings, details, 0

    def search_auctions(self, query, category_id, start, end):
        return list(self.listings)

    def get_details(self, item_id):
        self.calls += 1
        return self.details.get(item_id)


class FakeCards(CardApiClient):
    def __init__(self, rows=None, coverage_from=None, quota=False):
        super().__init__("test-key")
        self.rows, self.quota, self.queries = rows or [], quota, []
        self._coverage = coverage_from

    def sales(self, query, max_rows, page_size=200):
        self.queries.append(query)
        if self.quota:
            raise CardApiQuotaExceeded("Daily sales limit reached")
        self.coverage_from = self._coverage
        self.rows_used += len(self.rows) or 1
        return self.rows[:max_rows]


def options(**overrides):
    defaults = dict(run_at=RUN_AT, window_hours=12, comp_platform="ebay", max_rows_per_card=300)
    defaults.update(overrides)
    return RunOptions(**defaults)


def details(shipping=4.99, aspects=None, condition_id="2750"):
    return ListingDetails(aspects=dict(aspects or PSA10_ASPECTS), condition_id=condition_id, shipping_cost=shipping)


GOOD_TITLE = "2023 Prizm Victor Wembanyama #136 Rookie PSA 10"
TEN_SALES = [sale(p) for p in (38, 39, 40, 40, 41, 42, 40, 39, 41, 40)]


def test_good_listing_becomes_a_bid():
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details()})
    cards = FakeCards(TEN_SALES)
    result = run([WEMBY], ebay, cards, options())
    (e,) = result.evaluations
    assert e.is_bid, e.no_bid_reason
    assert e.summary.count == 10
    assert e.summary.median == 40
    assert e.decision.max_bid == pytest.approx(26.30 - 4.99)
    assert cards.queries[0].grader == "PSA" and cards.queries[0].grade == "10"
    assert cards.queries[0].q == "Victor Wembanyama prizm 136"


def test_comps_of_other_grades_and_parallels_do_not_count():
    rows = TEN_SALES[:7] + [
        sale(90, title="2023 Prizm Victor Wembanyama #136 Silver PSA 10"),
        sale(15, title="2023 Prizm Victor Wembanyama #136 PSA 9"),
        sale(40, title="2023 Prizm Victor Wembanyama #136 PSA 10", price_confirmed=False),
        sale(40, days_ago=45),
    ]
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details()})
    result = run([WEMBY], ebay, FakeCards(rows), options())
    (e,) = result.evaluations
    assert not e.is_bid
    assert "only 7 matching sold comps" in e.no_bid_reason
    assert e.comp_rejections == {"variant": 1, "grade": 1, "unconfirmed_price": 1, "outside_window": 1}


def test_price_already_too_high_skips_comps():
    ebay = FakeEbay([listing(1, GOOD_TITLE, price=60.0)], {"v1|1|0": details()})
    cards = FakeCards(TEN_SALES)
    (e,) = run([WEMBY], ebay, cards, options()).evaluations
    assert "highest possible max bid" in e.no_bid_reason
    assert cards.queries == []


def test_current_bid_above_max_bid_is_not_on_the_list():
    ebay = FakeEbay([listing(1, GOOD_TITLE, price=21.50)], {"v1|1|0": details()})
    (e,) = run([WEMBY], ebay, FakeCards(TEN_SALES), options()).evaluations
    assert e.decision.max_bid == pytest.approx(21.31)
    assert not e.is_bid
    assert "already above max bid" in e.no_bid_reason


def test_calculated_shipping_is_no_bid():
    ebay = FakeEbay([listing(1, GOOD_TITLE, shipping=None)], {"v1|1|0": details(shipping=None)})
    (e,) = run([WEMBY], ebay, FakeCards(TEN_SALES), options()).evaluations
    assert "shipping" in e.no_bid_reason


def test_missing_set_item_specific_is_no_bid():
    aspects = {k: v for k, v in PSA10_ASPECTS.items() if k != "Set"}
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details(aspects=aspects)})
    (e,) = run([WEMBY], ebay, FakeCards(TEN_SALES), options()).evaluations
    assert "Set item specific" in e.no_bid_reason


def test_same_card_is_comped_once():
    ebay = FakeEbay(
        [listing(1, GOOD_TITLE), listing(2, GOOD_TITLE + " invest", hours=3)],
        {"v1|1|0": details(), "v1|2|0": details()},
    )
    cards = FakeCards(TEN_SALES)
    result = run([WEMBY], ebay, cards, options())
    assert len(cards.queries) == 1
    assert all(e.is_bid for e in result.evaluations)


def test_number_dropped_from_query_when_nothing_found():
    class EmptyFirst(FakeCards):
        def sales(self, query, max_rows, page_size=200):
            rows = super().sales(query, max_rows, page_size)
            return [] if len(self.queries) == 1 else rows

    cards = EmptyFirst(TEN_SALES)
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details()})
    (e,) = run([WEMBY], ebay, cards, options()).evaluations
    assert [q.q for q in cards.queries] == ["Victor Wembanyama prizm 136", "Victor Wembanyama prizm"]
    assert e.is_bid


def test_quota_exhaustion_marks_remaining_cards():
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details()})
    (e,) = run([WEMBY], ebay, FakeCards(quota=True), options()).evaluations
    assert "allowance used up" in e.no_bid_reason


def test_short_card_api_window_stops_the_run():
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details()})
    cards = FakeCards(TEN_SALES, coverage_from=date(2026, 10, 7))
    with pytest.raises(ShortCompWindow, match="Builder"):
        run([WEMBY], ebay, cards, options())
    result = run([WEMBY], ebay, FakeCards(TEN_SALES, coverage_from=date(2026, 10, 7)),
                 options(allow_short_comp_window=True))
    assert result.comp_coverage_from == "2026-10-07"


def test_junk_and_excluded_titles_filtered_before_any_api_call():
    row = WatchlistRow("Victor Wembanyama", "Prizm", ("auto",), "261328")
    ebay = FakeEbay(
        [listing(1, "2023 Prizm Victor Wembanyama lot of 3"),
         listing(2, "2023 Prizm Victor Wembanyama Auto /25"),
         listing(3, "2023 Prizm Wemby #136 PSA 10")],
        {},
    )
    cards = FakeCards(TEN_SALES)
    result = run([row], ebay, cards, options())
    reasons = [e.no_bid_reason for e in result.evaluations]
    assert "lot" in reasons[0]
    assert "excluded word 'auto'" in reasons[1]
    assert "does not name" in reasons[2]
    assert cards.queries == []


def test_outputs(tmp_path):
    ebay = FakeEbay(
        [listing(1, GOOD_TITLE), listing(2, "2023 Prizm Victor Wembanyama lot of 3", hours=1)],
        {"v1|1|0": details()},
    )
    result = run([WEMBY], ebay, FakeCards(TEN_SALES), options())
    run_info = output.RunInfo("20261010T230000Z", RUN_AT, result.window_end, 1, result.comp_rows_used)

    bid_list = tmp_path / "bid_list.csv"
    assert output.write_bid_list(bid_list, result.evaluations) == 1
    with bid_list.open() as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0].keys()) == list(output.BID_LIST_COLUMNS)
    assert rows[0] == {
        "card_title": GOOD_TITLE,
        "item_url": "https://www.ebay.com/itm/1",
        "auction_end_time": "2026-10-11T01:00:00Z",
        "max_bid": "21.31",
        "comp_count": "10",
        "median_comp": "40.00",
        "predicted_profit": "4.00",
    }

    predictions = tmp_path / "logs" / "predictions.csv"
    output.append_predictions(predictions, run_info, result.evaluations)
    output.append_predictions(predictions, run_info, result.evaluations)
    with predictions.open() as handle:
        logged = list(csv.DictReader(handle))
    assert len(logged) == 4  # appended, not overwritten
    assert {r["decision"] for r in logged} == {"BID", "NO_BID"}
    assert all(r["actual_close_price"] == "" for r in logged)

    summary = output.append_run_summary(tmp_path / "logs" / "runs.csv", run_info, result.evaluations)
    assert summary["bids"] == 1 and summary["total_predicted_profit"] == "4.00"

    audit = tmp_path / "logs" / "comps" / "run.jsonl"
    output.write_comp_audit(audit, result.evaluations)
    (record,) = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(record["matched_comps"]) == 10


def test_bid_list_end_time_in_local_zone(tmp_path):
    from zoneinfo import ZoneInfo
    ebay = FakeEbay([listing(1, GOOD_TITLE)], {"v1|1|0": details()})
    result = run([WEMBY], ebay, FakeCards(TEN_SALES), options())
    path = tmp_path / "bid_list.csv"
    output.write_bid_list(path, result.evaluations, ZoneInfo("America/New_York"))
    assert "2026-10-10 21:00:00 EDT" in path.read_text()
