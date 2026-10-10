"""The Card API (thecardapi.com) client: real sold sales for comps.

REST endpoint GET /api/v1/market/sales, authenticated with the x-market-api-key
header. Every row returned counts against the plan's daily row allowance
(5,000/day on the free plan), so callers cap rows per card.

The plan's lookback window matters: the free plan only returns 3 days of sales
and Starter 14. A 30-day median needs Builder or higher. `date_from` older than
the plan allows is silently clamped, and `meta.coverage_date_from` reports the
real start, which this client exposes as `coverage_from`.
"""

import time
from dataclasses import dataclass
from datetime import date

import requests

BASE_URL = "https://www.thecardapi.com/api/v1/market"
MAX_PAGE_SIZE = 1000


class CardApiError(RuntimeError):
    pass


class CardApiQuotaExceeded(CardApiError):
    pass


@dataclass(frozen=True)
class SalesQuery:
    q: str
    date_from: date
    date_to: date | None = None
    platform: str | None = "ebay"
    graded: bool | None = None
    grader: str | None = None
    grade: str | None = None

    def params(self) -> dict[str, str]:
        params = {"q": self.q, "date_from": self.date_from.isoformat(), "sort": "date_desc"}
        if self.date_to:
            params["date_to"] = self.date_to.isoformat()
        if self.platform:
            params["platform"] = self.platform
        if self.graded is not None:
            params["graded"] = "true" if self.graded else "false"
        if self.grader:
            params["grader"] = self.grader
        if self.grade:
            params["grade"] = self.grade
        return params


class CardApiClient:
    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        retries: int = 3,
    ):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.rows_used = 0
        self.rows_remaining: str | None = None
        self.coverage_from: date | None = None

    def _get(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"x-market-api-key": self.api_key, "Accept": "application/json"}
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.retries:
                    raise CardApiError(f"The Card API unreachable: {exc}") from exc
                time.sleep(2 ** attempt)
                continue

            self.rows_remaining = response.headers.get("X-RateLimit-Remaining", self.rows_remaining)
            if response.status_code == 429:
                body = response.text[:300]
                if "limit" in body.lower() and "daily" in body.lower():
                    raise CardApiQuotaExceeded(f"The Card API daily row allowance is used up: {body}")
                if attempt == self.retries:
                    raise CardApiQuotaExceeded(f"The Card API keeps rate-limiting (429): {body}")
                time.sleep(_retry_after(response, default=5.0 * attempt))
                continue
            if response.status_code in (401, 403):
                raise CardApiError(
                    f"The Card API refused the request ({response.status_code}): {response.text[:300]}. "
                    "Check CARD_API_KEY and your plan."
                )
            if response.status_code >= 500 and attempt < self.retries:
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                raise CardApiError(f"The Card API error {response.status_code}: {response.text[:300]}")
            try:
                return response.json()
            except ValueError as exc:
                raise CardApiError("The Card API returned invalid JSON") from exc
        raise CardApiError("The Card API request failed after retries")

    def sales(self, query: SalesQuery, max_rows: int, page_size: int = 200) -> list[dict]:
        """Most recent sales matching `query`, newest first, at most `max_rows` rows."""
        params = query.params()
        params["limit"] = str(min(page_size, max_rows, MAX_PAGE_SIZE))
        rows: list[dict] = []
        while len(rows) < max_rows:
            payload = self._get("/sales", params)
            page = payload.get("data") or []
            self.rows_used += max(len(page), 1)  # the API bills at least one unit per request
            rows.extend(page)
            self._note_coverage(payload.get("meta") or {})
            next_cursor = (payload.get("pagination") or {}).get("next_cursor")
            if not page or not next_cursor:
                break
            params = {**params, "cursor": next_cursor}
            params["limit"] = str(min(page_size, max_rows - len(rows), MAX_PAGE_SIZE))
        return rows[:max_rows]

    def _note_coverage(self, meta: dict) -> None:
        raw = meta.get("coverage_date_from")
        if not raw:
            return
        try:
            covered = date.fromisoformat(str(raw)[:10])
        except ValueError:
            return
        if self.coverage_from is None or covered > self.coverage_from:
            self.coverage_from = covered


def _retry_after(response: requests.Response, default: float) -> float:
    try:
        return min(float(response.headers.get("Retry-After", default)), 60.0)
    except ValueError:
        return default
