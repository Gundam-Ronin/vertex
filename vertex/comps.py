"""Fetch sold comps for one card identity from The Card API and filter them to the same card."""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from .card_identity import CardIdentity, CompRecord, comp_mismatch, set_words
from .cardapi import CardApiClient, SalesQuery


@dataclass
class CompResult:
    query: str
    returned: int
    matched: list[CompRecord] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)


def build_query(identity: CardIdentity, include_number: bool = True) -> str:
    """Plain words only: the API ANDs every word, so each one must be in a matching title."""
    parts = [identity.player or "", *set_words(identity.set_name)]
    if include_number and identity.card_number:
        parts.append(identity.card_number)
    return " ".join(p for p in parts if p).strip()


def _grade_filters(identity: CardIdentity) -> dict:
    if not identity.graded:
        return {"graded": False}
    numeric = identity.grade.split()[0]
    return {
        "graded": True,
        "grader": identity.grader,
        "grade": None if numeric == "AUTH" else numeric,
    }


def _parse_sale_date(row: dict) -> date | None:
    raw = row.get("sale_date") or row.get("sold_at")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def to_comp(row: dict, cutoff: date, platform: str | None) -> tuple[CompRecord | None, str | None]:
    """Turn an API row into a CompRecord, or say why it is not a real, in-window sale."""
    if row.get("price_confirmed") is False:
        return None, "unconfirmed_price"
    raw_price = row.get("price") if row.get("price") is not None else row.get("sale_price")
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        return None, "no_price"
    if price <= 0:
        return None, "no_price"
    currency = row.get("currency")
    if currency and str(currency).upper() != "USD":
        return None, "not_usd"
    if platform and row.get("platform") and str(row["platform"]).lower() != platform.lower():
        return None, "other_platform"
    sold_on = _parse_sale_date(row)
    if sold_on is None or sold_on < cutoff:
        return None, "outside_window"
    title = row.get("title")
    if not title:
        return None, "no_title"
    return CompRecord(
        title=title,
        price=price,
        sold_at=sold_on.isoformat(),
        platform=row.get("platform"),
        listing_type=row.get("listing_type"),
        listing_url=row.get("listing_url"),
        grader=row.get("grader") or row.get("grading_company"),
        grade=None if row.get("grade") is None else str(row.get("grade")),
        card_number=row.get("card_number"),
    ), None


def fetch_comps(
    client: CardApiClient,
    identity: CardIdentity,
    cutoff: date,
    platform: str | None,
    max_rows: int,
) -> CompResult:
    """Query with the card number first; if the API finds nothing, retry without it.

    The retry covers titles where the number is written in a form the search
    does not tokenize the same way. Local matching still requires the number.
    """
    grade_filters = _grade_filters(identity)
    rows: list[dict] = []
    query = build_query(identity, include_number=True)
    for include_number in (True, False):
        if not include_number and not identity.card_number:
            break
        query = build_query(identity, include_number=include_number)
        if len(query) < 4:
            return CompResult(query=query, returned=0, rejections={"query_too_short": 1})
        rows = client.sales(SalesQuery(q=query, date_from=cutoff, platform=platform, **grade_filters), max_rows)
        if rows:
            break

    seen: set[str] = set()
    result = CompResult(query=query, returned=len(rows))
    rejections: Counter[str] = Counter()
    for row in rows:
        key = str(row.get("id") or row.get("listing_url") or id(row))
        if key in seen:
            rejections["duplicate"] += 1
            continue
        seen.add(key)
        comp, reason = to_comp(row, cutoff, platform)
        if comp is None:
            rejections[reason] += 1
            continue
        mismatch = comp_mismatch(identity, comp)
        if mismatch:
            rejections[mismatch] += 1
            continue
        result.matched.append(comp)
    result.rejections = dict(rejections)
    return result
