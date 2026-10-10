import pytest

from vertex.card_identity import (
    CompRecord,
    comp_mismatch,
    extract_card_number,
    extract_variants,
    extract_years,
    grade_from_title,
    identity_from_listing,
    junk_reason,
)

WEMBY = "Victor Wembanyama"


def comp(title, **fields):
    return CompRecord(title=title, price=50.0, sold_at="2026-10-01T00:00:00Z", **fields)


def identity(title, aspects=None, condition_id=None, player=WEMBY):
    ident, reason = identity_from_listing(title, player, aspects, condition_id)
    assert reason is None, reason
    return ident


@pytest.mark.parametrize(
    "title, grader, grade",
    [
        ("2023 Prizm Victor Wembanyama #136 PSA 10 GEM MINT", "PSA", "10"),
        ("2023 Prizm Wembanyama #136 BGS 9.5", "BGS", "9.5"),
        ("2023 Prizm Wembanyama #136 BGS 10 Black Label", "BGS", "10 BL"),
        ("2018 Topps Update Acuna #US250 PSA 9 (OC)", "PSA", "9 OC"),
        ("1999 Upper Deck Kobe #1 SGC 96", "SGC", "96"),
        ("2023 Prizm Wembanyama PSA slab", "PSA", "UNKNOWN"),
        ("2023 Prizm Wembanyama #136 RC", None, None),
        ("2023 Prizm Wembanyama #136 PSA 10 Candidate?", None, None),
        ("2023 Prizm Wembanyama #136 PSA 10?", None, None),
        ("2023 Prizm Wembanyama #136 potential PSA 10", None, None),
        ("2023 Prizm Wembanyama auto PSA/DNA certified", None, None),
        ("2023 PRIZM WEMBANYAMA #136 PSA 10 ROOKIE OF THE YEAR", "PSA", "10"),
    ],
)
def test_grade_from_title(title, grader, grade):
    parsed = grade_from_title(title)
    assert (parsed.grader, parsed.grade) == (grader, grade)


def test_seasons_count_both_years():
    assert extract_years("2023-24 Panini Prizm") == {2023, 2024}
    assert extract_years("23-24 Prizm Wemby") == {2023, 2024}
    assert extract_years("1999-00 Upper Deck") == {1999, 2000}
    assert extract_years("2023 Prizm") == {2023}


def test_card_number_ignores_serial_numbering_and_no_reserve():
    assert extract_card_number("Prizm Wemby #136 Gold") == "136"
    assert extract_card_number("Prizm Wemby Gold #/10") is None
    assert extract_card_number("Prizm Wemby Gold #3/10") is None
    assert extract_card_number("NO 2023 Prizm Wemby") is None
    assert extract_card_number("Topps Update #US250") == "US250"
    assert extract_card_number("Topps #007") == "7"


@pytest.mark.parametrize(
    "title, expected",
    [
        ("2023 Prizm Victor Wembanyama #136 RC", set()),
        ("2023 Prizm Victor Wembanyama #136 Silver Prizm RC", {"silver"}),
        ("2023 Prizm Wembanyama #136 Gold 3/10 PSA 10", {"gold", "/10"}),
        ("2023 Prizm Wembanyama #136 Gold /10", {"gold", "/10"}),
        ("2023 Prizm Wembanyama #136 Gold/10", {"gold", "/10"}),
        ("2023 Prizm Wembanyama #136 Gold numbered to 10", {"gold", "/10"}),
        ("2023 Prizm Wembanyama Black Finite 1/1", {"black", "1of1"}),
        ("2023 Prizm Wembanyama #136 BGS 10 Black Label", set()),
        ("2023 National Treasures Wembanyama RPA /99", {"auto", "mem", "/99"}),
        ("2023 Bowman Chrome 1st Auto Refractor /499", {"1st", "auto", "refractor", "/499"}),
        ("2019 Topps Chrome Vladimir Guerrero Jr Blue Jays #14", set()),
        ("2023-24 Prizm Wembanyama #136", set()),
    ],
)
def test_extract_variants(title, expected):
    assert extract_variants(title, WEMBY) == frozenset(expected)


def test_player_name_words_are_not_variants():
    assert extract_variants("2017 Prizm Derrick White #10 Silver", "Derrick White") == {"silver"}


@pytest.mark.parametrize(
    "title",
    ["Wembanyama Prizm lot of 5", "Wemby RP reprint", "Wemby you pick", "Wemby Prizm x3",
     "Wemby Prizm creased", "2023 Prizm Hobby Box sealed", "Wemby custom card"],
)
def test_junk_titles(title):
    assert junk_reason(title)


def test_fast_break_parallel_is_not_junk():
    assert junk_reason("2023 Prizm Wembanyama Fast Break Silver") is None


class TestListingIdentity:
    def test_graded_from_item_specifics(self):
        ident = identity(
            "2023 Prizm Victor Wembanyama #136 RC Gem Mint",
            aspects={"Graded": "Yes", "Professional Grader": "Professional Sports Authenticator (PSA)",
                     "Grade": "10", "Card Number": "136", "Season": "2023-24", "Set": "2023-24 Panini Prizm"},
            condition_id="2750",
        )
        assert (ident.grader, ident.grade, ident.card_number, ident.year) == ("PSA", "10", "136", 2023)

    def test_ungraded_listing_hedged_title_is_raw(self):
        ident = identity("2023 Prizm Victor Wembanyama #136 PSA 10 ready", condition_id="4000")
        assert not ident.graded

    def test_ungraded_listing_with_graded_title_is_rejected(self):
        ident, reason = identity_from_listing(
            "2023 Prizm Victor Wembanyama #136 PSA 10", WEMBY, {}, "4000")
        assert ident is None and "ungraded" in reason

    def test_graded_listing_without_grade_is_rejected(self):
        ident, reason = identity_from_listing(
            "2023 Prizm Victor Wembanyama #136 slabbed", WEMBY, {"Graded": "Yes"}, "2750")
        assert ident is None

    def test_parallel_aspect_adds_variant(self):
        ident = identity("2023 Prizm Victor Wembanyama #136", aspects={"Parallel/Variety": "Silver Prizm"})
        assert ident.variants == {"silver"}

    def test_base_parallel_aspect_adds_nothing(self):
        ident = identity("2023 Prizm Victor Wembanyama #136", aspects={"Parallel/Variety": "Base"})
        assert ident.variants == frozenset()


class TestCompMatching:
    base_psa10 = staticmethod(lambda: identity("2023 Panini Prizm Victor Wembanyama #136 RC PSA 10"))

    def test_same_card_matches(self):
        assert comp_mismatch(self.base_psa10(), comp("2023-24 Prizm Victor Wembanyama Rookie #136 PSA 10 GEM MT")) is None

    def test_grade_from_structured_fields(self):
        assert comp_mismatch(
            self.base_psa10(), comp("2023 Prizm Victor Wembanyama #136", grader="PSA", grade="10")) is None

    @pytest.mark.parametrize(
        "title, reason",
        [
            ("2023 Prizm Victor Wembanyama #136 PSA 9", "grade"),
            ("2023 Prizm Victor Wembanyama #136 BGS 10", "grade"),
            ("2023 Prizm Victor Wembanyama #136 RC", "grade"),
            ("2023 Prizm Victor Wembanyama #136 Silver PSA 10", "variant"),
            ("2023 Prizm Victor Wembanyama #137 PSA 10", "card_number"),
            ("2023 Prizm Victor Wembanyama PSA 10", "card_number"),
            ("2024 Prizm Victor Wembanyama #136 PSA 10", "year"),
            ("2023 Prizm Wembanyama #136 PSA 10", "player"),
            ("2023 Prizm Victor Wembanyama #136 PSA 10 lot of 2", "junk"),
            ("2023 Prizm Victor Wembanyama #136 PSA slab", "grade"),
        ],
    )
    def test_mismatches(self, title, reason):
        assert comp_mismatch(self.base_psa10(), comp(title)) == reason

    def test_raw_listing_rejects_graded_and_candidate_comps_are_raw(self):
        raw = identity("2023 Prizm Victor Wembanyama #136 RC")
        assert comp_mismatch(raw, comp("2023 Prizm Victor Wembanyama #136 PSA 10")) == "grade"
        assert comp_mismatch(raw, comp("2023 Prizm Victor Wembanyama #136 PSA 10 candidate")) is None
        assert comp_mismatch(raw, comp("2023 Prizm Victor Wembanyama #136", grader="PSA", grade="10")) == "grade"
