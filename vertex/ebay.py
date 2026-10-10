"""eBay Browse API client: auctions ending tonight, their item specifics, and how they ended.

Read-only. Uses an application token (client-credentials grant), which can
search and read listings but can never bid or buy.

Browse allows 5,000 calls a day per application by default. Search pages are
cheap (200 listings each), but item specifics need one getItem call per
listing, because the bulk getItems method is partner-only and omits them.
"""

import base64
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from .models import Listing

log = logging.getLogger(__name__)

API_ROOT = "https://api.ebay.com"
TOKEN_URL = f"{API_ROOT}/identity/v1/oauth2/token"
SEARCH_URL = f"{API_ROOT}/buy/browse/v1/item_summary/search"
ITEM_URL = f"{API_ROOT}/buy/browse/v1/item/"
SCOPE = "https://api.ebay.com/oauth/api_scope"
PAGE_SIZE = 200          # Browse maximum per page
MAX_RESULTS = 10_000     # Browse ceiling on offset + limit
GONE_ERROR_IDS = {11001, 11004}  # item not found / no longer available


class EbayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ListingDetails:
    aspects: dict[str, str]
    condition_id: str | None
    shipping_cost: float | None


@dataclass(frozen=True)
class Outcome:
    """How an ended auction finished, as far as eBay still reports it."""

    status: str  # "sold", "unsold", "running", or "gone" (no longer readable)
    price: float | None = None
    bid_count: int | None = None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _money(amount: dict | None) -> tuple[float | None, str | None]:
    if not amount or amount.get("value") in (None, ""):
        return None, None
    try:
        return float(amount["value"]), amount.get("currency")
    except (TypeError, ValueError):
        return None, None


def cheapest_shipping(options: list[dict] | None) -> float | None:
    """Cheapest fixed-cost shipping option to the US; None if there is none.

    Calculated shipping is ignored on purpose: the buy rule gives no bid when
    the shipping charge is not known up front.
    """
    costs = []
    for option in options or []:
        if str(option.get("shippingCostType", "")).upper() == "CALCULATED":
            continue
        cost, currency = _money(option.get("shippingCost"))
        if cost is not None and currency in (None, "USD"):
            costs.append(cost)
    return min(costs) if costs else None


def _clean_name(name: str) -> str:
    # Seller-entered names sometimes carry eBay's numeric id: "Card Condition (40001)".
    name = name.strip()
    if name.endswith(")") and "(" in name and name[name.rindex("(") + 1:-1].isdigit():
        name = name[: name.rindex("(")].strip()
    return name


def flatten_aspects(item: dict) -> dict[str, str]:
    """Merge item specifics and condition descriptors into one name -> value map."""
    aspects: dict[str, str] = {}
    for aspect in item.get("localizedAspects") or []:
        name, value = aspect.get("name"), aspect.get("value")
        if name and value:
            aspects[_clean_name(name)] = str(value).strip().rstrip(":")
    for descriptor in item.get("conditionDescriptors") or []:
        name = descriptor.get("name")
        values = [v.get("content") for v in descriptor.get("values") or [] if v.get("content")]
        if name and values:
            aspects[_clean_name(name)] = str(values[0]).strip().rstrip(":")
    return aspects


def listing_from_summary(summary: dict) -> Listing | None:
    end_raw = summary.get("itemEndDate")
    if not end_raw or not summary.get("itemId"):
        return None
    if "AUCTION" not in (summary.get("buyingOptions") or []):
        return None
    price, currency = _money(summary.get("currentBidPrice"))
    if price is None:
        price, currency = _money(summary.get("price"))
    return Listing(
        item_id=summary["itemId"],
        legacy_item_id=summary.get("legacyItemId"),
        title=summary.get("title", ""),
        url=summary.get("itemWebUrl", ""),
        end_time=parse_time(end_raw),
        current_price=price,
        currency=currency or "USD",
        bid_count=int(summary.get("bidCount") or 0),
        shipping_cost=cheapest_shipping(summary.get("shippingOptions")),
        condition_id=summary.get("conditionId"),
    )


class EbayClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        marketplace: str = "EBAY_US",
        session: requests.Session | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.now = now
        self._token: str | None = None
        self._token_expires = 0.0
        self.calls = 0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        response = self.session.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=self.timeout,
        )
        if not response.ok:
            raise EbayError(
                f"eBay OAuth failed ({response.status_code}): {response.text[:300]}. "
                "Check EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (production keyset)."
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + float(payload.get("expires_in", 7200))
        return self._token

    def _get(self, url: str, params: dict[str, str], allow_gone: bool = False) -> dict | None:
        """GET with retries. Returns None for a listing eBay says is gone, if allowed."""
        for attempt in range(1, self.retries + 1):
            headers = {
                "Authorization": f"Bearer {self._access_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                "Accept": "application/json",
            }
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.retries:
                    raise EbayError(f"eBay unreachable: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            self.calls += 1
            if response.status_code == 401 and attempt < self.retries:
                self._token = None  # expired token; mint a fresh one
                continue
            if (response.status_code == 429 or response.status_code >= 500) and attempt < self.retries:
                time.sleep(2 ** attempt)
                continue
            if response.ok:
                return response.json()
            if allow_gone and _error_ids(response) & GONE_ERROR_IDS:
                return None
            raise EbayError(f"eBay Browse API error {response.status_code}: {response.text[:300]}")
        raise EbayError("eBay request failed after retries")

    def search_auctions(self, query: str, category_id: str, start: datetime, end: datetime) -> list[Listing]:
        """Auctions matching `query` that end between `start` and `end`, soonest first."""
        # eBay rejects an end-date range that starts in the past.
        start = max(start, self.now() + timedelta(minutes=1))
        if start >= end:
            return []
        params = {
            "q": query,
            "category_ids": category_id,
            "filter": f"buyingOptions:{{AUCTION}},itemEndDate:[{_iso(start)}..{_iso(end)}],deliveryCountry:US",
            "sort": "endingSoonest",
            "limit": str(PAGE_SIZE),
        }
        listings: list[Listing] = []
        offset = 0
        while True:
            payload = self._get(SEARCH_URL, {**params, "offset": str(offset)})
            _raise_on_filter_warnings(payload, query)
            for summary in payload.get("itemSummaries") or []:
                listing = listing_from_summary(summary)
                if listing is not None:
                    listings.append(listing)
            offset += PAGE_SIZE
            if not payload.get("next"):
                break
            if offset + PAGE_SIZE > MAX_RESULTS:
                log.warning("'%s' has more than %d auctions in the window; only the first %d were read. "
                            "Narrow it with the watchlist's include column or a shorter --hours.",
                            query, MAX_RESULTS, offset)
                break
        return listings

    def get_item(self, item_id: str, compact: bool = False) -> dict | None:
        params = {"fieldgroups": "COMPACT"} if compact else {}
        return self._get(ITEM_URL + quote(item_id, safe=""), params, allow_gone=True)

    def get_details(self, item_id: str) -> ListingDetails | None:
        """Item specifics, condition and shipping for one listing (one API call)."""
        item = self.get_item(item_id)
        if item is None:
            return None
        return ListingDetails(
            aspects=flatten_aspects(item),
            condition_id=item.get("conditionId"),
            shipping_cost=cheapest_shipping(item.get("shippingOptions")),
        )

    def outcome(self, item_id: str) -> Outcome:
        """How an auction ended. eBay keeps ended listings readable for a few days.

        A sold listing reads OUT_OF_STOCK with a sold quantity, and its price is
        the final bid. An auction that closed without a winner stays IN_STOCK.
        """
        item = self.get_item(item_id, compact=True)
        if item is None:
            return Outcome("gone")
        availability = (item.get("estimatedAvailabilities") or [{}])[0]
        price, _ = _money(item.get("currentBidPrice"))
        if price is None:
            price, _ = _money(item.get("price"))
        bids = item.get("bidCount")
        bid_count = int(bids) if bids is not None else None
        end_raw = item.get("itemEndDate")
        if end_raw and parse_time(end_raw) > self.now():
            return Outcome("running", price, bid_count)
        if availability.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
            if int(availability.get("estimatedSoldQuantity") or 0) > 0:
                return Outcome("sold", price, bid_count)
        return Outcome("unsold", None, bid_count)


def _error_ids(response: requests.Response) -> set[int]:
    try:
        errors = response.json().get("errors") or []
    except (ValueError, AttributeError):
        return set()
    return {e.get("errorId") for e in errors if isinstance(e, dict)}


def _raise_on_filter_warnings(payload: dict, query: str) -> None:
    """eBay drops an invalid filter with only a warning (HTTP 200). Never search unfiltered."""
    for warning in payload.get("warnings") or []:
        if warning.get("errorId") == 12002 or "filter" in str(warning.get("message", "")).lower():
            raise EbayError(f"eBay ignored a search filter for '{query}': {warning}")
