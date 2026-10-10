import csv
from datetime import datetime, timedelta, timezone

from vertex.cardapi import CardApiClient
from vertex.ebay import Outcome
from vertex.output import PREDICTION_COLUMNS
from vertex.reconcile import reconcile

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


class FakeEbay:
    def __init__(self, outcomes):
        self.outcomes, self.asked = outcomes, []

    def outcome(self, item_id):
        self.asked.append(item_id)
        return self.outcomes.get(item_id.split("|")[1], Outcome("gone"))


class FakeCards(CardApiClient):
    def __init__(self, rows):
        super().__init__("key")
        self.rows, self.queries = rows, []

    def sales(self, query, max_rows, page_size=200):
        self.queries.append(query)
        return self.rows


def prediction(item_id, max_bid="21.31", end=NOW - timedelta(days=3), **extra):
    row = {c: "" for c in PREDICTION_COLUMNS}
    row.update({
        "run_id": "r1", "item_id": item_id, "auction_end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "comp_query": "Victor Wembanyama prizm 136", "median_comp": "40.00",
        "inbound_shipping": "4.99", "max_bid": max_bid, "price_at_scan": "12.00", "bids_at_scan": "3",
        "decision": "BID" if max_bid else "NO_BID",
    })
    row.update(extra)
    return row


def write(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PREDICTION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def read(path):
    with path.open(newline="") as handle:
        return {r["item_id"] + "/" + r["run_id"]: r for r in csv.DictReader(handle)}


def test_scores_from_ebay_then_card_api(tmp_path):
    path = tmp_path / "predictions.csv"
    write(path, [
        prediction("387100000111"),                                            # sold under our max
        prediction("387100000111", run_id="r2"),                               # same auction, later run
        prediction("387100000222"),                                            # sold over our max
        prediction("387100000223", max_bid="33.00"),                           # sold exactly at our max
        prediction("387100000333"),                                            # gone from eBay, in Card API
        prediction("387100000444", end=NOW - timedelta(days=10)),              # never found
        prediction("387100000555", end=NOW - timedelta(minutes=5)),            # just ended
        prediction("387100000666", bids_at_scan="0", price_at_scan="9.99"),    # no bids: win at opening
        prediction("387100000777", max_bid="", reason="next bid above max"),   # no max bid
        prediction("387100000888", median_comp="", comp_query=""),             # never comped
    ])
    ebay = FakeEbay({
        "387100000111": Outcome("sold", 18.00, 7),
        "387100000222": Outcome("sold", 33.00, 12),
        "387100000223": Outcome("sold", 33.00, 12),
        "387100000666": Outcome("unsold", None, None),
        "387100000777": Outcome("sold", 25.00, 4),
    })
    cards = FakeCards([{"listing_url": "https://www.ebay.com/itm/wemby-prizm/387100000333?x=1",
                        "price": 19.50, "bids": 5}])
    counts = reconcile(path, ebay, cards, NOW)
    rows = read(path)

    sold = rows["387100000111/r1"]
    assert (sold["actual_close_price"], sold["actual_bid_count"], sold["close_source"]) == ("18.00", "7", "ebay")
    assert sold["would_have_won"] == "possibly"
    # 40 - 5.30 - 0.40 - 4.00 = 30.30 net, minus 18.50 (close + $0.50 increment) and 4.99 shipping
    assert sold["profit_at_close"] == "6.81"
    assert rows["387100000111/r2"]["actual_close_price"] == "18.00"
    assert ebay.asked.count("v1|387100000111|0") == 1

    assert rows["387100000222/r1"]["would_have_won"] == "no"
    assert rows["387100000222/r1"]["profit_at_close"] == ""
    assert rows["387100000223/r1"]["would_have_won"] == "no"

    assert rows["387100000333/r1"]["close_source"] == "card_api"
    assert rows["387100000333/r1"]["actual_close_price"] == "19.50"

    assert rows["387100000444/r1"]["close_source"] == "not_found"
    assert rows["387100000555/r1"]["close_source"] == ""

    unsold = rows["387100000666/r1"]
    assert unsold["close_source"] == "ebay_unsold" and unsold["actual_close_price"] == ""
    assert unsold["would_have_won"] == "possibly"
    assert unsold["profit_at_close"] == f"{30.30 - 9.99 - 4.99:.2f}"

    assert rows["387100000777/r1"]["actual_close_price"] == "25.00"
    assert rows["387100000777/r1"]["would_have_won"] == ""
    assert "v1|387100000888|0" not in ebay.asked

    assert counts == {"scored": 6, "unsold": 1, "not_found": 1, "pending": 1}


def test_card_api_not_asked_until_it_has_indexed_the_sale(tmp_path):
    path = tmp_path / "predictions.csv"
    write(path, [prediction("387100000333", end=NOW - timedelta(hours=3))])
    cards = FakeCards([])
    reconcile(path, FakeEbay({}), cards, NOW)
    assert cards.queries == []
    assert read(path)["387100000333/r1"]["close_source"] == ""


def test_already_scored_rows_are_left_alone(tmp_path):
    path = tmp_path / "predictions.csv"
    write(path, [prediction("387100000111", actual_close_price="18.00", close_source="ebay")])
    ebay = FakeEbay({})
    reconcile(path, ebay, None, NOW)
    assert ebay.asked == []
    assert read(path)["387100000111/r1"]["actual_close_price"] == "18.00"


def test_reserve_not_met_is_not_a_win_at_the_opening_price(tmp_path):
    path = tmp_path / "predictions.csv"
    write(path, [prediction("387100000666", bids_at_scan="0", price_at_scan="9.99")])
    reconcile(path, FakeEbay({"387100000666": Outcome("unsold", None, 6)}), None, NOW)
    row = read(path)["387100000666/r1"]
    assert row["close_source"] == "ebay_unsold"
    assert row["would_have_won"] == "" and row["profit_at_close"] == ""


def test_profit_at_close_never_pays_more_than_max_bid(tmp_path):
    path = tmp_path / "predictions.csv"
    write(path, [prediction("387100000111", max_bid="21.31")])
    reconcile(path, FakeEbay({"387100000111": Outcome("sold", 21.00, 9)}), None, NOW)
    row = read(path)["387100000111/r1"]
    assert row["would_have_won"] == "possibly"
    assert row["profit_at_close"] == f"{30.30 - 21.31 - 4.99:.2f}"
