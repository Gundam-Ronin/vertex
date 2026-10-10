"""End-to-end: python -m vertex against simulated eBay and Card API servers."""

import csv
from datetime import datetime, timedelta, timezone

import pytest

from vertex import __main__ as cli
from vertex import cardapi, ebay

from .fakes import FakeResponse

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Router:
    """One fake session for both APIs, answering by URL."""

    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        return FakeResponse(payload={"access_token": "tok", "expires_in": 7200})

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/item_summary/search"):
            return FakeResponse(payload={"itemSummaries": [{
                "itemId": "v1|387100000111|0",
                "legacyItemId": "387100000111",
                "title": "2023 Panini Prizm Victor Wembanyama #136 RC PSA 10",
                "itemWebUrl": "https://www.ebay.com/itm/387100000111",
                "itemEndDate": iso(NOW + timedelta(hours=3)),
                "buyingOptions": ["AUCTION"],
                "currentBidPrice": {"value": "15.00", "currency": "USD"},
                "bidCount": 2,
                "shippingOptions": [{"shippingCostType": "FIXED",
                                     "shippingCost": {"value": "4.99", "currency": "USD"}}],
                "conditionId": "2750",
            }]})
        if "/buy/browse/v1/item/" in url:
            return FakeResponse(payload={
                "itemId": "v1|387100000111|0",
                "conditionId": "2750",
                "localizedAspects": [
                    {"name": "Set", "value": "2023-24 Panini Prizm"},
                    {"name": "Card Number", "value": "136"},
                    {"name": "Season", "value": "2023-24"},
                ],
                "conditionDescriptors": [
                    {"name": "Professional Grader", "values": [{"content": "Professional Sports Authenticator (PSA)"}]},
                    {"name": "Grade", "values": [{"content": "10"}]},
                ],
                "shippingOptions": [{"shippingCostType": "FIXED",
                                     "shippingCost": {"value": "4.99", "currency": "USD"}}],
            })
        if url.endswith("/market/sales"):
            sold = (NOW - timedelta(days=4)).date().isoformat()
            return FakeResponse(payload={
                "data": [{"id": f"ebay-{i}", "platform": "eBay", "price": p, "currency": "USD",
                          "price_confirmed": True, "sale_date": sold, "listing_type": "auction",
                          "title": "2023-24 Prizm Victor Wembanyama #136 Rookie PSA 10 GEM MINT",
                          "listing_url": f"https://www.ebay.com/itm/38700000{i}"}
                         for i, p in enumerate([38, 39, 40, 40, 41, 42, 40, 39, 41, 40, 250])],
                "pagination": {"next_cursor": None},
                "meta": {"coverage_date_from": (NOW - timedelta(days=30)).date().isoformat()},
            })
        raise AssertionError(f"unexpected GET {url}")


@pytest.fixture
def router(monkeypatch, tmp_path):
    shared = Router()
    monkeypatch.setattr(ebay.requests, "Session", lambda: shared)
    monkeypatch.setattr(cardapi.requests, "Session", lambda: shared)
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("CARD_API_KEY", "tca_test")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "watchlist.csv").write_text("player,include,exclude,category_id\nVictor Wembanyama,Prizm,,\n")
    return shared


def test_nightly_run_writes_bid_list_and_logs(router, tmp_path):
    assert cli.main([]) == 0

    with (tmp_path / "bid_list.csv").open() as handle:
        (bid,) = list(csv.DictReader(handle))
    assert bid["card_title"] == "2023 Panini Prizm Victor Wembanyama #136 RC PSA 10"
    assert bid["median_comp"] == "40.00"
    assert bid["comp_count"] == "10"  # the $250 sale was trimmed as an outlier
    assert bid["max_bid"] == "21.31"
    assert bid["predicted_profit"] == "4.00"

    with (tmp_path / "logs" / "predictions.csv").open() as handle:
        (logged,) = list(csv.DictReader(handle))
    assert logged["decision"] == "BID" and logged["item_id"] == "387100000111"
    assert (tmp_path / "logs" / "runs.csv").exists()
    assert list((tmp_path / "logs" / "comps").glob("*.jsonl"))

    sales_call = next(params for url, params in router.calls if url.endswith("/market/sales"))
    assert sales_call["q"] == "Victor Wembanyama prizm 136"
    assert sales_call["grader"] == "PSA" and sales_call["grade"] == "10"


def test_missing_keys(monkeypatch, router):
    monkeypatch.delenv("CARD_API_KEY")
    with pytest.raises(SystemExit, match="CARD_API_KEY"):
        cli.main([])
