"""Work out which exact card a listing is, and whether a sold comp is the same card.

A watchlist row names a player, but a player has hundreds of distinct cards.
Comping a PSA 10 against raw sales, or a base card against a /25 parallel, is
the fastest way to overpay, so matching fails closed: when in doubt the comp is
rejected, and a listing whose identity is unclear gets no bid.

A comp counts only if it matches the listing on all of:
  * player name (every name token present)
  * year (when the listing has one)
  * card number (when the listing has one)
  * set and insert name words (from the listing's "Set" and "Insert Set" item specifics)
  * grading company and grade, or both raw
  * the exact set of parallel/variant markers (colors, refractor, auto, /99, ...)
"""

import re
import unicodedata
from dataclasses import dataclass, field

GRADERS = (
    "PSA", "BGS", "BVG", "BCCG", "SGC", "CGC", "CSG", "HGA", "TAG",
    "ISA", "GMA", "KSA", "MNT", "AGS",
)
_GRADER_ALT = "|".join(GRADERS)

# Full names eBay uses in the "Professional Grader" descriptor/aspect.
_GRADER_NAMES = (
    ("professional sports authenticator", "PSA"),
    ("beckett vintage", "BVG"),
    ("beckett collectors club", "BCCG"),
    ("beckett", "BGS"),
    ("sportscard guaranty", "SGC"),
    ("certified sports guaranty", "CSG"),
    ("certified guaranty", "CGC"),
    ("hybrid grading", "HGA"),
    ("international sports authentication", "ISA"),
)

UNKNOWN_GRADE = "UNKNOWN"

_GRADE_RE = re.compile(
    rf"\b({_GRADER_ALT})\s*"
    r"(?:gem\s*(?:mint|mt)|mint|mt|nm[\s-]?mt|pristine|black\s*label|gold\s*label)?\s*"
    r"(10|[1-9](?:\.5)?|\d{2,3}|auth(?:entic)?(?:\s*altered)?)\b"
    r"(?:\s*\(?(?-i:(OC|MC|ST|PD|MK|OF))\)?(?!\w))?",
    re.IGNORECASE,
)
# A grader named with no readable grade ("PSA slab"). PSA/DNA is autograph
# authentication on raw cards, so it does not count.
_BARE_GRADER_RE = re.compile(rf"\b({_GRADER_ALT})\b(?!\s*/?\s*dna)", re.IGNORECASE)
# Raw cards advertised as grading candidates ("PSA 10 ready?", "potential BGS 9.5").
_HEDGE_RE = re.compile(
    rf"\b(?:{_GRADER_ALT})\s*(?:10|[1-9](?:\.5)?)?\s*(?:\?|candidate|ready|worthy|quality|potential|possible)"
    rf"|\b(?:potential|possible|future|likely|could\s+be|should\s+be|not)\s+(?:a\s+)?(?:{_GRADER_ALT})\b"
    r"|\bnot\s+graded\b|\bungraded\b|\braw\b",
    re.IGNORECASE,
)

_JUNK_PATTERNS = (
    r"\blots?\b", r"\bbundle\b", r"\b(?:you|u)[\s-]*pick\b", r"\bpick\s+(?:your|ur)\b",
    r"\bchoose\b", r"\bcomplete\s+set\b", r"\bteam\s+set\b",
    r"\b(?:case|box|pack|group)\s+break\b", r"\brandom\b", r"\bmystery\b",
    r"\breprints?\b", r"\brp\b", r"\bcustom\b", r"\bdigital\b", r"\bfacsimile\b",
    r"\bproxy\b", r"\bnovelty\b", r"\breplica\b", r"\baceo\b",
    r"\bx\s?[2-9]\b", r"\b[2-9]\s?x\b", r"\bsealed\b",
    r"\b(?:hobby|blaster|mega|hanger|cello|fat|value|retail)\s+(?:box|pack)\b",
    r"\bcrease[sd]?\b", r"\bdamaged?\b", r"\bpoor\b", r"\bfiller\b", r"\bas[\s-]is\b",
)
_JUNK_RE = re.compile("|".join(_JUNK_PATTERNS), re.IGNORECASE)

# Team names containing color words, stripped before variant detection.
_TEAM_PHRASES = re.compile(
    r"\b(?:red\s+sox|white\s+sox|blue\s+jays|red\s+wings|blue\s+jackets|golden\s+knights"
    r"|golden\s+state|green\s+bay)\b"
)

# Multi-word or synonym patterns -> canonical variant tokens.
_VARIANT_RULES = tuple(
    (re.compile(pattern), tokens)
    for pattern, tokens in (
        (r"\brpa\b", ("auto", "mem")),
        (r"\bauto(?:graph(?:ed)?|s)?\b|\bsigned\b|\bsignatures?\b", ("auto",)),
        (r"\bpatch(?:es)?\b|\brelic\b|\bjersey\b|\bmemorabilia\b|\bswatch\b|\bgame[\s-]?(?:used|worn)\b", ("mem",)),
        (r"\bone\s+of\s+one\b|\bsuperfractor\b", ("1of1",)),
        (r"\bprinting\s+plates?\b|\bplates?\b", ("plate",)),
        (r"\bx[\s-]?fractor\b", ("xfractor",)),
        (r"\brefractors?\b", ("refractor",)),
        (r"\breverse\s+holo\b", ("reverse-holo",)),
        (r"\bholo(?:foil|graphic)?\b", ("holo",)),
        (r"\bcracked\s+ice\b", ("cracked-ice",)),
        (r"\bfast\s+break\b", ("fast-break",)),
        (r"\btie[\s-]?dye\b", ("tie-dye",)),
        (r"\bsnake\s?skin\b", ("snakeskin",)),
        (r"\bstained\s+glass\b", ("stained-glass",)),
        (r"\bcolor\s+blast\b", ("color-blast",)),
        (r"\bshort\s+print\b|\bsp\b", ("sp",)),
        (r"\bssp\b", ("ssp",)),
        (r"\bcase\s+hit\b", ("case-hit",)),
        (r"\b(?:photo\s+|image\s+)?variation\b|\bvar\b", ("variation",)),
        (r"\b1st\b|\bfirst\s+edition\b", ("1st",)),
        (r"\bshadowless\b", ("shadowless",)),
        (r"\bpremier\s+level\b", ("premier-level",)),
    )
)
# Single words that mark a parallel. Set names (Prizm, Chrome, Mosaic, Optic)
# are deliberately absent: they are shared by base cards and parallels alike.
_VARIANT_WORDS = frozenset({
    "silver", "gold", "red", "blue", "green", "orange", "purple", "pink", "black",
    "white", "bronze", "teal", "yellow", "aqua", "platinum", "ruby", "sapphire",
    "emerald", "copper", "lime", "neon", "maroon", "magenta", "fuchsia", "navy",
    "foil", "shimmer", "mojo", "wave", "disco", "hyper", "scope", "velocity",
    "pulsar", "laser", "sparkle", "atomic", "lava", "speckle", "camo", "tiger",
    "zebra", "choice", "kaboom", "downtown", "nebula", "galactic", "cosmic",
    "logoman", "concourse", "courtside", "vinyl", "prism", "rainbow", "glitter",
    "flash", "genesis", "negative", "sepia",
})

_SEASON_RE = re.compile(r"\b((?:19|20)\d{2})\s*[-/]\s*(\d{2}|\d{4})\b")
_SHORT_SEASON_RE = re.compile(r"(?<![\w/#])(\d{2})-(\d{2})(?![\w/])")
_YEAR_RE = re.compile(r"\b(19[4-9]\d|20[0-4]\d)\b")
_PRINT_RUN_RE = re.compile(r"(?:(?<![\w.])\d{1,4}\s*)?/\s*(\d{1,4})\b")
_NUMBERED_TO_RE = re.compile(r"\b(?:numbered|#'?d)\s+(?:to\s+|out\s+of\s+)?(\d{1,4})\b")
_CARD_NUMBER_RE = re.compile(
    r"(#|\bno\.?\s*|\bcard\s*#?\s*)\s*([a-z]{0,5}-?\d{1,4}[a-z]?)\b(?!\s*/)",
    re.IGNORECASE,
)
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
# Words in eBay "Set" values that sellers routinely leave out of titles.
_SET_STOPWORDS = {
    "panini", "basketball", "football", "baseball", "hockey", "soccer", "racing",
    "nba", "nfl", "mlb", "nhl", "wnba", "ufc", "trading", "cards", "card", "the",
    "and", "edition", "base", "set",
}


def fold(text: str) -> str:
    """Lowercase and strip accents, so 'Acuña' matches 'Acuna'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def name_tokens(player: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", fold(player))
    return [t for t in tokens if t not in _NAME_SUFFIXES]


def set_words(set_name: str | None) -> list[str]:
    """Distinctive words of an eBay Set value: '2023-24 Panini Prizm Basketball' -> ['prizm']."""
    if not set_name:
        return []
    words = re.findall(r"[a-z]+", fold(set_name))
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in _SET_STOPWORDS]


def has_player(title: str, player: str | None) -> bool:
    if not player:
        return True
    words = set(re.findall(r"[a-z0-9]+", fold(title)))
    return all(t in words for t in name_tokens(player))


def junk_reason(title: str) -> str | None:
    match = _JUNK_RE.search(title)
    return f"title looks like a lot/reprint/damaged card ('{match.group(0)}')" if match else None


def extract_years(text: str) -> set[int]:
    """Every year a title mentions; a season like 2023-24 counts as 2023 and 2024."""
    folded = fold(text)
    years = {int(y) for y in _YEAR_RE.findall(folded)}
    for start, end in _SEASON_RE.findall(folded):
        if len(end) == 2:
            years.add(int(start[:2] + end) if end >= start[2:] else int(start) + 1)
        else:
            years.add(int(end))
    for start, end in _SHORT_SEASON_RE.findall(folded):
        if int(end) == (int(start) + 1) % 100:
            first = 2000 + int(start) if int(start) < 50 else 1900 + int(start)
            years.update({first, first + 1})
    return years


def primary_year(text: str) -> int | None:
    """The first year written; for a season like 2023-24 that is 2023."""
    folded = fold(text)
    match = _YEAR_RE.search(folded)
    if match:
        return int(match.group(1))
    years = extract_years(folded)
    return min(years) if years else None


def normalize_card_number(raw: str | None) -> str | None:
    if not raw:
        return None
    value = re.sub(r"^(?:#|no\.?|card)\s*", "", raw.strip(), flags=re.IGNORECASE)
    value = value.split("/")[0].lstrip("#").strip().upper()
    if not value or value in {"N/A", "NA", "NONE", "UNKNOWN"}:
        return None
    if value.isdigit():
        value = str(int(value))
    return value


def extract_card_number(title: str) -> str | None:
    for match in _CARD_NUMBER_RE.finditer(_SEASON_RE.sub(" ", title)):
        prefix, value = match.groups()
        if prefix != "#" and _YEAR_RE.fullmatch(value):
            continue  # "No 2023 ..." is not card number 2023
        return normalize_card_number(value)
    return None


def grader_from_name(value: str | None) -> str | None:
    if not value:
        return None
    acronym = re.search(rf"\b({_GRADER_ALT})\b", value, re.IGNORECASE)
    if acronym:
        return acronym.group(1).upper()
    lowered = fold(value)
    for needle, code in _GRADER_NAMES:
        if needle in lowered:
            return code
    return None


def normalize_grade(grader: str, grade: str, title: str = "", qualifier: str | None = None) -> str:
    """'GEM MT 10' -> '10'; BGS black label 10 -> '10 BL'; CGC pristine 10 -> '10 PRISTINE'."""
    value = grade.strip().upper()
    match = re.search(r"AUTH|\d+(?:\.5)?", value)
    value = match.group(0) if match else value
    lowered = fold(title)
    if value == "10" and grader == "BGS" and "black label" in lowered:
        value = "10 BL"
    elif value == "10" and grader == "CGC" and "pristine" in lowered:
        value = "10 PRISTINE"
    if qualifier:
        value = f"{value} {qualifier.upper()}"
    return value


@dataclass(frozen=True)
class TitleGrade:
    grader: str | None
    grade: str | None
    hedged: bool


def grade_from_title(title: str) -> TitleGrade:
    if _HEDGE_RE.search(title):
        return TitleGrade(None, None, True)
    match = _GRADE_RE.search(title)
    if match:
        grader = match.group(1).upper()
        return TitleGrade(grader, normalize_grade(grader, match.group(2), title, match.group(3)), False)
    bare = _BARE_GRADER_RE.search(title)
    if bare:
        return TitleGrade(bare.group(1).upper(), UNKNOWN_GRADE, False)
    return TitleGrade(None, None, False)


def extract_variants(title: str, player: str | None = None) -> frozenset[str]:
    text = fold(title)
    text = _SEASON_RE.sub(" ", text)
    text = _GRADE_RE.sub(" ", text)
    text = re.sub(r"\b(?:black|gold)\s+label\b|\bpristine\b", " ", text)
    text = _TEAM_PHRASES.sub(" ", text)
    if player:
        for token in name_tokens(player):
            text = re.sub(rf"\b{re.escape(token)}\b", " ", text)

    tokens: set[str] = set()
    runs = _PRINT_RUN_RE.findall(text) + _NUMBERED_TO_RE.findall(text)
    for run in runs:
        tokens.add("1of1" if int(run) == 1 else f"/{int(run)}")
    text = _NUMBERED_TO_RE.sub(" ", _PRINT_RUN_RE.sub(" ", text))
    for pattern, canonical in _VARIANT_RULES:
        if pattern.search(text):
            tokens.update(canonical)
            text = pattern.sub(" ", text)
    tokens.update(w for w in re.findall(r"[a-z]+", text) if w in _VARIANT_WORDS)
    return frozenset(tokens)


@dataclass(frozen=True)
class CardIdentity:
    player: str | None
    year: int | None
    card_number: str | None
    grader: str | None
    grade: str | None
    variants: frozenset[str] = field(default_factory=frozenset)
    set_name: str | None = None
    insert_name: str | None = None

    @property
    def graded(self) -> bool:
        return self.grader is not None

    @property
    def grade_label(self) -> str:
        return f"{self.grader} {self.grade}" if self.graded else "raw"

    @property
    def required_words(self) -> list[str]:
        """Set and insert words every comp title must contain."""
        return list(dict.fromkeys(set_words(self.set_name) + set_words(self.insert_name)))

    def key(self) -> tuple:
        return (
            fold(self.player or ""), self.year, self.card_number, self.grader, self.grade,
            tuple(sorted(self.variants)), tuple(self.required_words),
        )

    def describe(self) -> str:
        parts = [str(self.year) if self.year else "", self.set_name or "", self.insert_name or "", self.player or ""]
        if self.card_number:
            parts.append(f"#{self.card_number}")
        if self.variants:
            parts.append("[" + ", ".join(sorted(self.variants)) + "]")
        parts.append(self.grade_label)
        return " ".join(p for p in parts if p)


def _aspect(aspects: dict[str, str], *names: str) -> str | None:
    lowered = {k.strip().lower(): v for k, v in aspects.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value and value.strip():
            return value.strip()
    return None


def identity_from_listing(
    title: str,
    player: str | None,
    aspects: dict[str, str] | None = None,
    condition_id: str | None = None,
) -> tuple[CardIdentity | None, str | None]:
    """Build the identity of an auction from its title and eBay item specifics.

    `aspects` merges localizedAspects and conditionDescriptors (name -> value).
    Returns (identity, None) or (None, why it cannot be identified safely).
    """
    aspects = aspects or {}

    graded_aspect = (_aspect(aspects, "Graded") or "").lower()
    ebay_says_graded = condition_id == "2750" or graded_aspect == "yes"
    ebay_says_raw = condition_id == "4000" or graded_aspect == "no"
    if ebay_says_graded and ebay_says_raw:
        return None, "eBay item specifics disagree on whether the card is graded"
    title_grade = grade_from_title(title)
    title_grade_known = title_grade.grade not in (None, UNKNOWN_GRADE)

    grader = grade = None
    if ebay_says_graded:
        grader = grader_from_name(_aspect(aspects, "Professional Grader", "Grader")) or title_grade.grader
        raw_grade = _aspect(aspects, "Grade") or (title_grade.grade if title_grade_known else None)
        if not grader or not raw_grade:
            return None, "graded listing but grader or grade is missing"
        grade = normalize_grade(grader, raw_grade, title)
        if title_grade_known and title_grade.grader == grader and title_grade.grade.split()[0] == grade.split()[0]:
            grade = title_grade.grade  # keeps qualifiers like "9 OC" that item specifics omit
    elif ebay_says_raw:
        if title_grade.grader:
            return None, f"eBay says ungraded but title says {title_grade.grader} {title_grade.grade}"
    elif title_grade.grader:
        if not title_grade_known:
            return None, f"title names {title_grade.grader} but no readable grade"
        grader, grade = title_grade.grader, title_grade.grade

    season = _aspect(aspects, "Season", "Year Manufactured", "Year")
    year = primary_year(title) or (primary_year(season) if season else None)

    card_number = normalize_card_number(_aspect(aspects, "Card Number")) or extract_card_number(title)

    variants = set(extract_variants(title, player))
    parallel = _aspect(aspects, "Parallel/Variety")
    if parallel and fold(parallel) not in {"base", "n/a", "none", "na"}:
        variants |= extract_variants(parallel, player)
    if (_aspect(aspects, "Autographed") or "").lower() == "yes":
        variants.add("auto")

    identity = CardIdentity(
        player=player,
        year=year,
        card_number=card_number,
        grader=grader,
        grade=grade,
        variants=frozenset(variants),
        set_name=_aspect(aspects, "Set"),
        insert_name=_aspect(aspects, "Insert Set"),
    )
    return identity, None


@dataclass(frozen=True)
class CompRecord:
    """One sold sale as reported by The Card API, reduced to what matching needs."""

    title: str
    price: float
    sold_at: str
    platform: str | None = None
    listing_type: str | None = None
    listing_url: str | None = None
    grader: str | None = None
    grade: str | None = None
    card_number: str | None = None


def comp_grade(comp: CompRecord) -> tuple[str | None, str | None]:
    """Grade of a sold comp: structured fields first, then the title."""
    grader = grader_from_name(comp.grader) or grader_from_name(comp.grade)
    if grader and comp.grade and re.search(r"\d|auth", comp.grade, re.IGNORECASE):
        return grader, normalize_grade(grader, comp.grade, comp.title)
    title_grade = grade_from_title(comp.title)
    if grader and not title_grade.grader:
        return grader, UNKNOWN_GRADE
    return title_grade.grader, title_grade.grade


def comp_mismatch(identity: CardIdentity, comp: CompRecord) -> str | None:
    """Return why `comp` is not the same card as `identity`, or None if it matches."""
    if junk_reason(comp.title):
        return "junk"
    if not has_player(comp.title, identity.player):
        return "player"
    if identity.year and identity.year not in extract_years(comp.title):
        return "year"
    comp_words = set(re.findall(r"[a-z]+", fold(comp.title)))
    if not all(w in comp_words for w in identity.required_words):
        return "set"
    if identity.card_number:
        comp_number = normalize_card_number(comp.card_number) or extract_card_number(comp.title)
        if comp_number != identity.card_number:
            return "card_number"
    if comp_grade(comp) != (identity.grader, identity.grade):
        return "grade"
    if extract_variants(comp.title, identity.player) != identity.variants:
        return "variant"
    return None
