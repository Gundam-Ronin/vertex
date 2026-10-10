from datetime import date, datetime, timezone

import pytest

from vertex import cardapi, ebay
from vertex.cardapi import CardApiClient, CardApiError, CardApiQuotaExceeded, SalesQuery

from .fakes import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(cardapi.time, "sleep", lambda s: None)
    monkeypatch.setattr(ebay.time, "sleep", lambda s: None)


QUERY = SalesQuery(q="Victor Wembanyama prizm 136", date_from=date(2026, 9, 10),
                   graded=True, grader="PSA", grade="10")


class TestCardApi:
    def test_paginates_with_cursor_and_tracks_usage(self):
        session = FakeSession(get=[
            FakeResponse(payload={"data": [{"id": 1}, {"id": 2}],
                                  "pagination": {"next_cursor": "abc"},
                                  "meta": {"coverage_date_from": "2026-09-10"}},
                         headers={"X-RateLimit-Remaining": "4998"}),
            FakeResponse(payload={"data": [{"id": 3}], "pagination": {"next_cursor": None}}),
        ])
        client = CardApiClient("key", session=session)
        rows = client.sales(QUERY, max_rows=300)
        assert [r["id"] for r in rows] == [1, 2, 3]
        assert client.rows_used == 3
        assert client.rows_remaining == "4998"
        assert client.coverage_from == date(2026, 9, 10)
        first, second = session.get_calls
        assert first["url"] == "https://www.thecardapi.com/api/v1/market/sales"
        assert first["headers"]["x-market-api-key"] == "key"
        assert first["params"] == {
            "q": "Victor Wembanyama prizm 136", "date_from": "2026-09-10", "sort": "date_desc",
            "platform": "ebay", "graded": "true", "grader": "PSA", "grade": "10", "limit": "200",
        }
        assert second["params"]["cursor"] == "abc"

    def test_stops_at_max_rows(self):
        session = FakeSession(get=[
            FakeResponse(payload={"data": [{"id": i} for i in range(5)], "pagination": {"next_cursor": "x"}}),
        ])
        rows = CardApiClient("key", session=session).sales(QUERY, max_rows=5)
        assert len(rows) == 5
        assert session.get_calls[0]["params"]["limit"] == "5"

    def test_daily_limit_raises_quota_error(self):
        session = FakeSession(get=[FakeResponse(429, text="Daily sales limit reached. Upgrade your plan for more.")])
        with pytest.raises(CardApiQuotaExceeded):
            CardApiClient("key", session=session).sales(QUERY, 100)

    def test_burst_429_is_retried(self):
        session = FakeSession(get=[
            FakeResponse(429, text="Too many requests", headers={"Retry-After": "1"}),
            FakeResponse(payload={"data": [], "pagination": {}}),
        ])
        assert CardApiClient("key", session=session).sales(QUERY, 100) == []

    def test_bad_key(self):
        session = FakeSession(get=[FakeResponse(401, text="Invalid API key")])
        with pytest.raises(CardApiError, match="CARD_API_KEY"):
            CardApiClient("key", session=session).sales(QUERY, 100)


def _token():
    return FakeResponse(payload={"access_token": "tok", "expires_in": 7200})


def _summary(n, **extra):
    summary = {
        "itemId": f"v1|{n}|0",
        "legacyItemId": str(n),
        "title": f"Card {n}",
        "itemWebUrl": f"https://www.ebay.com/itm/{n}",
        "itemEndDate": "2026-10-11T01:00:00.000Z",
        "buyingOptions": ["AUCTION"],
        "currentBidPrice": {"value": "12.50", "currency": "USD"},
        "bidCount": 4,
        "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "4.99", "currency": "USD"}}],
        "conditionId": "2750",
    }
    summary.update(extra)
    return summary


START = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 11, 11, 0, tzinfo=timezone.utc)


def client_with(get=(), post=None, now=datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)):
    session = FakeSession(post=post if post is not None else [_token()], get=get)
    return ebay.EbayClient("id", "secret", session=session, now=lambda: now), session


class TestEbay:
    def test_search_builds_auction_window_filter_and_pages(self):
        client, session = client_with(get=[
            FakeResponse(payload={"total": 201, "next": "more", "itemSummaries": [_summary(1)]}),
            FakeResponse(payload={"total": 201, "itemSummaries": [_summary(2)]}),
        ])
        listings = client.search_auctions("Victor Wembanyama Prizm", "261328", START, END)

        assert [l.item_id for l in listings] == ["v1|1|0", "v1|2|0"]
        first = session.get_calls[0]
        assert first["params"]["filter"] == (
            "buyingOptions:{AUCTION},itemEndDate:[2026-10-10T23:00:00.000Z..2026-10-11T11:00:00.000Z],"
            "deliveryCountry:US"
        )
        assert first["params"]["sort"] == "endingSoonest"
        assert first["params"]["category_ids"] == "261328"
        assert first["headers"]["Authorization"] == "Bearer tok"
        assert first["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
        assert session.get_calls[1]["params"]["offset"] == "200"
        assert session.post_calls[0]["data"]["grant_type"] == "client_credentials"

        listing = listings[0]
        assert listing.current_price == 12.50 and listing.bid_count == 4
        assert listing.shipping_cost == 4.99
        assert listing.end_time == datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)

    def test_window_start_is_never_in_the_past(self):
        client, session = client_with(get=[FakeResponse(payload={"itemSummaries": []})],
                                      now=datetime(2026, 10, 10, 23, 30, tzinfo=timezone.utc))
        client.search_auctions("x", "261328", START, END)
        assert "itemEndDate:[2026-10-10T23:31:00.000Z.." in session.get_calls[0]["params"]["filter"]

    def test_ignored_filter_warning_is_fatal(self):
        client, _ = client_with(get=[FakeResponse(payload={
            "itemSummaries": [_summary(1)],
            "warnings": [{"errorId": 12002, "message": "The 'itemEndDate' value is invalid."}],
        })])
        with pytest.raises(ebay.EbayError, match="ignored a search filter"):
            client.search_auctions("x", "261328", START, END)

    def test_non_auctions_are_dropped(self):
        client, _ = client_with(get=[FakeResponse(payload={
            "itemSummaries": [_summary(1), _summary(2, buyingOptions=["FIXED_PRICE"])]})])
        assert [l.item_id for l in client.search_auctions("x", "261328", START, END)] == ["v1|1|0"]

    def test_shipping(self):
        fixed = {"shippingCostType": "FIXED", "shippingCost": {"value": "1.50", "currency": "USD"}}
        free = {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}
        calculated = {"shippingCostType": "CALCULATED"}
        assert ebay.cheapest_shipping([fixed, free]) == 0.0
        assert ebay.cheapest_shipping([calculated]) is None
        assert ebay.cheapest_shipping([]) is None
        assert ebay.cheapest_shipping([calculated, fixed]) == 1.50

    def test_details_from_get_item(self):
        item = {
            "itemId": "v1|1|0",
            "conditionId": "2750",
            "localizedAspects": [{"name": "Set", "value": "2023-24 Panini Prizm"},
                                 {"name": "Card Number", "value": "136"}],
            "conditionDescriptors": [
                {"name": "Professional Grader", "values": [{"content": "Professional Sports Authenticator (PSA)"}]},
                {"name": "Grade", "values": [{"content": "10"}]},
                {"name": "Card Condition (40001)", "values": [{"content": "Near Mint or Better:"}]},
            ],
            "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "3.00", "currency": "USD"}}],
        }
        client, session = client_with(get=[FakeResponse(payload=item)])
        d = client.get_details("v1|1|0")
        assert session.get_calls[0]["url"] == "https://api.ebay.com/buy/browse/v1/item/v1%7C1%7C0"
        assert d.aspects == {"Set": "2023-24 Panini Prizm", "Card Number": "136",
                             "Professional Grader": "Professional Sports Authenticator (PSA)", "Grade": "10",
                             "Card Condition": "Near Mint or Better"}
        assert d.condition_id == "2750"
        assert d.shipping_cost == 3.00

    def test_gone_item_returns_none(self):
        client, _ = client_with(get=[FakeResponse(404, payload={"errors": [{"errorId": 11001}]})])
        assert client.get_details("v1|1|0") is None

    def test_other_errors_raise(self):
        client, _ = client_with(get=[FakeResponse(400, payload={"errors": [{"errorId": 12345}]})])
        with pytest.raises(ebay.EbayError):
            client.get_details("v1|1|0")

    @pytest.mark.parametrize(
        "payload, expected",
        [
            ({"currentBidPrice": {"value": "31.00"}, "bidCount": 9, "itemEndDate": "2026-10-11T01:00:00.000Z",
              "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "OUT_OF_STOCK", "estimatedSoldQuantity": 1}]},
             ebay.Outcome("sold", 31.0, 9)),
            ({"price": {"value": "9.99"}, "itemEndDate": "2026-10-11T01:00:00.000Z",
              "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}]},
             ebay.Outcome("unsold", None, None)),
            ({"price": {"value": "9.99"}, "itemEndDate": "2026-10-13T01:00:00.000Z",
              "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK"}]},
             ebay.Outcome("running", 9.99, None)),
        ],
    )
    def test_outcome(self, payload, expected):
        client, session = client_with(get=[FakeResponse(payload=payload)],
                                      now=datetime(2026, 10, 12, tzinfo=timezone.utc))
        assert client.outcome("v1|1|0") == expected
        assert session.get_calls[0]["params"] == {"fieldgroups": "COMPACT"}

    def test_outcome_gone(self):
        client, _ = client_with(get=[FakeResponse(404, payload={"errors": [{"errorId": 11004}]})])
        assert client.outcome("v1|1|0") == ebay.Outcome("gone")

    def test_expired_token_is_refreshed(self):
        client, session = client_with(
            post=[_token(), _token()],
            get=[FakeResponse(401, text="expired"), FakeResponse(payload={"itemSummaries": []})],
        )
        assert client.search_auctions("x", "261328", START, END) == []
        assert len(session.post_calls) == 2

    def test_bad_credentials(self):
        client, _ = client_with(post=[FakeResponse(401, text="invalid_client")])
        with pytest.raises(ebay.EbayError, match="EBAY_CLIENT_ID"):
            client.search_auctions("x", "261328", START, END)
