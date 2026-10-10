"""Outlier trimming, median, and the comp-count gate (SPEC.md Sections 3 and 11.4)."""

import statistics
from dataclasses import dataclass
from fractions import Fraction

from .config import COMP_RULE, CompRule


@dataclass(frozen=True)
class CompSummary:
    raw_count: int
    kept: tuple[float, ...]
    lower_fence: float | None
    upper_fence: float | None

    @property
    def count(self) -> int:
        return len(self.kept)

    @property
    def median(self) -> float | None:
        return float(statistics.median(_cents(p) for p in self.kept)) if self.kept else None


def _cents(price: float) -> Fraction:
    # Exact arithmetic: in floats a price sitting exactly on a fence can land a hair outside it.
    return Fraction(round(price * 100), 100)


def summarize(prices: list[float], rule: CompRule = COMP_RULE) -> CompSummary:
    """Drop prices outside Tukey fences (Q1 - k*IQR, Q3 + k*IQR), k = 1.5."""
    ordered = sorted(_cents(p) for p in prices)
    if len(ordered) < 4:
        # Quartiles are meaningless this small; the 8-comp gate rejects it anyway.
        return CompSummary(len(ordered), tuple(float(p) for p in ordered), None, None)
    q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    spread = (q3 - q1) * Fraction(str(rule.iqr_multiplier))
    lower, upper = q1 - spread, q3 + spread
    kept = tuple(float(p) for p in ordered if lower <= p <= upper)
    return CompSummary(len(ordered), kept, float(lower), float(upper))


def passes_gate(summary: CompSummary, rule: CompRule = COMP_RULE) -> bool:
    """At least `min_comps` real sales must survive trimming."""
    return summary.count >= rule.min_comps
