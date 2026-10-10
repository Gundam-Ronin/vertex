"""Read watchlist.csv: one row per player (or set) to scan each night.

Columns:
  player       Player name. Every word must appear in a listing title and in each comp.
               Leave blank for a set-level row driven by `include`.
  include      Optional extra search words, e.g. "Prizm" or "2023 Bowman Chrome".
  exclude      Optional words to skip, separated by semicolons, e.g. "auto;patch".
  category_id  Optional eBay category. Blank means Sports Trading Card Singles (261328).

Rows whose player starts with "#" are ignored, so you can comment one out.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CATEGORY_ID

COLUMNS = ("player", "include", "exclude", "category_id")


@dataclass(frozen=True)
class WatchlistRow:
    player: str
    include: str
    exclude: tuple[str, ...]
    category_id: str

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.player, self.include) if p)

    @property
    def search_query(self) -> str:
        return self.label

    def excluded_by(self, title: str) -> str | None:
        lowered = title.lower()
        for word in self.exclude:
            if re.search(rf"\b{re.escape(word.lower())}\b", lowered):
                return word
        return None


def load_watchlist(path: Path) -> list[WatchlistRow]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = {"player"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing required column(s): {', '.join(sorted(missing))}")
        rows = []
        for line_number, record in enumerate(reader, start=2):
            player = (record.get("player") or "").strip()
            include = (record.get("include") or "").strip()
            if player.startswith("#") or not (player or include):
                continue
            exclude = tuple(w.strip() for w in (record.get("exclude") or "").split(";") if w.strip())
            category_id = (record.get("category_id") or "").strip() or DEFAULT_CATEGORY_ID
            if not category_id.isdigit():
                raise ValueError(f"{path} line {line_number}: category_id must be numeric, got {category_id!r}")
            rows.append(WatchlistRow(player, include, exclude, category_id))
    if not rows:
        raise ValueError(f"{path} has no active rows")
    return rows
