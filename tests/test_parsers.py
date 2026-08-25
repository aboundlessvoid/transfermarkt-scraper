"""Offline unit tests for the page parsers, against saved HTML fixtures.

These make **no HTTP requests**. `test_crawlers.py` hits the live site and is the right
place to notice that Transfermarkt has changed its markup; this file is the right place
to notice that a parser has stopped extracting a value it used to extract, which is a
different failure and the one that actually went unnoticed for seven seasons.

The fixtures under `tests/fixtures/` are byte-faithful fragments of pages fetched on
2026-08-20, one per shape the parser has to handle.

Every assertion here is about a **value**, never about a key being present. Two real
defects — `half_time_score` being the literal `"("` on 540,832 records, and all three
market-value fields being NULL on 1.23 M — survived a test suite that asserted
`"half_time_score" in game`. A key-existence assertion is one a broken scraper passes.
"""

import pathlib

import pytest
from parsel import Selector

from tfmkt.crawlers.games import RESULT_TYPES, extract_result_annotation
from tfmkt.crawlers.players import extract_current_market_value

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name):
    return Selector(text=(FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# games.py — the half-time score and the result annotation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected_half_time,expected_result_type", [
    # "(<span>0:</span>2)" — three text nodes. The whole defect lives here: reading
    # only the first one yields "(" and no score is ever scraped.
    ("game_result_normal.html", "0:2", None),
    # The same element, carrying a decision annotation instead of a score.
    ("game_result_aet.html", None, "AET"),
    ("game_result_pens.html", None, "on pens"),
    ("game_result_walkover.html", None, "uncontested"),
    # The same element with the digits absent -- "( : )" in non-breaking spaces -- which
    # is the site saying it has no half-time score for this game. NEITHER field is set:
    # a two-way "score, else annotation" branch reports this as a result_type of ":".
    ("game_result_no_half_time.html", None, None),
])
def test_result_annotation(name, expected_half_time, expected_result_type):
    result_box = fixture(name).css("div.ergebnis-wrap")
    half_time, result_type = extract_result_annotation(result_box)
    assert half_time == expected_half_time
    assert result_type == expected_result_type


def test_half_time_is_never_a_bare_bracket():
    """The regression itself, stated as the thing that must not come back.

    Pinned separately from the parametrised case above because this is the *shape* rule
    the corpus violated 540,832 times, and it should keep failing loudly even if the
    expected score in the fixture is ever updated.
    """
    for name in ("game_result_normal.html", "game_result_aet.html",
                 "game_result_pens.html", "game_result_walkover.html",
                 "game_result_no_half_time.html"):
        half_time, result_type = extract_result_annotation(
            fixture(name).css("div.ergebnis-wrap"))
        assert half_time != "(", f"{name}: the pre-fix defect is back"
        if half_time is not None:
            assert half_time.count(":") == 1 and half_time.replace(":", "").isdigit(),                 f"{name}: half_time_score is not a score: {half_time!r}"
        # The same rule for the other half of the split. `test_crawlers` already asserts
        # this, but only against whatever games a live crawl happens to fetch -- and
        # empty-bracket games are ~1.4% of them, so a small sample sails straight past.
        # Offline fixtures are where a value domain can actually be pinned.
        assert result_type is None or result_type in RESULT_TYPES.values(),             f"{name}: result_type outside its domain: {result_type!r}"


def test_empty_bracket_is_not_an_annotation():
    """The regression this file exists to prevent, stated as its own rule.

    Stripping the brackets off "( : )" leaves ":", and a catch-all fall-through wrote
    exactly that into `result_type` on 1,259 records in seasons 2025-2026. Every partial
    variant below was observed in the corpus too, so none may come back either.
    """
    for markup in ('(<span>&nbsp;:</span>&nbsp;)',   # neither side printed
                   '( : )',                          # the same, plain spaces
                   '(<span>1:</span>)',              # away side missing
                   '(<span>:</span>0)'):             # home side missing
        sel = Selector(text=(
            f'<div class="ergebnis-wrap"><div class="sb-halbzeit">{markup}</div></div>'))
        half_time, result_type = extract_result_annotation(sel.css("div.ergebnis-wrap"))
        assert half_time is None, f"{markup}: invented a half-time score {half_time!r}"
        assert result_type is None,             f"{markup}: score debris written to result_type as {result_type!r}"


def test_result_annotation_absent():
    """A result box with no annotation at all yields two Nones, not a crash."""
    sel = Selector(text='<div class="ergebnis-wrap"><div class="sb-endstand">0:0</div></div>')
    assert extract_result_annotation(sel.css("div.ergebnis-wrap")) == (None, None)


def test_result_annotation_tolerates_whitespace_and_split_nodes():
    """The score may be split across any number of nodes, with newlines between them.

    Transfermarkt splits it into two today. Nothing guarantees it stays two, and the
    parser must not depend on the count — which is precisely what `.get()` did.
    """
    sel = Selector(text=(
        '<div class="ergebnis-wrap"><div class="sb-halbzeit">'
        '(\n <span>1</span><span>:</span>\n<span>0</span>\n)</div></div>'
    ))
    assert extract_result_annotation(sel.css("div.ergebnis-wrap")) == ("1:0", None)


# ---------------------------------------------------------------------------
# players.py — the current market value
# ---------------------------------------------------------------------------

def test_current_market_value():
    """Split across three text nodes for the same reason `sb-halbzeit` is."""
    value, last_update = extract_current_market_value(
        fixture("player_header_market_value.html"))
    assert value == "€220.00m"
    assert last_update == "22/07/2026"


def test_current_market_value_absent():
    """A player with no valuation has no wrapper element at all — verified live.

    This must be `(None, None)` and not an exception: it is the common case, since most
    of the 457 k players in the wide-tail corpus are youth or lower-tier.
    """
    assert extract_current_market_value(
        fixture("player_header_no_market_value.html")) == (None, None)


def _live_string_constants(module):
    """Every string literal in `module` that is not a docstring.

    Docstrings are excluded deliberately: this file's own explanation of *why* the old
    selectors were removed names them, and a plain substring scan would flag the
    explanation as the offence.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def test_market_value_dead_selectors_are_gone():
    """The old selectors matched markup Transfermarkt has deleted.

    Asserting their absence keeps someone from "restoring" them on the theory that the
    fields going NULL was a regression in this fork rather than a change at the source.
    """
    from tfmkt.crawlers import players

    live = _live_string_constants(players)
    for dead in ("tm-player-market-value-development__current-value",
                 "tm-player-market-value-development__max-value"):
        assert not any(dead in s for s in live), f"dead selector reintroduced: {dead}"
    assert not hasattr(players, "parse_market_history"), (
        "parse_market_history scraped an inline Highcharts series that the site no "
        "longer emits; market value history now comes from the market-value harvest"
    )
