"""Offline tests for the book search behind the answer model's search tool.

The search is pure: ``search_text`` takes the ``text.json`` payload of one
synced book and returns ranked paragraphs. No library, no network and no trace
is involved here.
"""

from __future__ import annotations

import pytest

from voice_bridge_server import search
from voice_bridge_server.search import book_search, search_text, terms, tokenise

PARAGRAPHS = [
    "The train was delayed by snow in the mountains.",
    "Poirot looked at the passengers one by one.",
    "Ratchett said he had never seen the man before.",
    "The detective asked about the compartment door.",
    "Snow fell all night and the train did not move.",
]


def book(paragraphs: list[str] | None = None) -> dict:
    """Return one ``text.json`` payload with its offsets computed."""

    texts = list(PARAGRAPHS if paragraphs is None else paragraphs)
    offsets: list[int] = []
    total = 0
    for paragraph in texts:
        offsets.append(total)
        total += len(paragraph)
    return {"paragraphs": texts, "cum_chars": offsets}


def total_chars(text: dict) -> int:
    return text["cum_chars"][-1] + len(text["paragraphs"][-1])


# Tokens --------------------------------------------------------------------


def test_tokens_are_lower_case_alphanumeric_words() -> None:
    assert tokenise("Who's Afraid of the 42 Snow?") == [
        "who",
        "s",
        "afraid",
        "of",
        "the",
        "42",
        "snow",
    ]


def test_stop_words_are_dropped() -> None:
    assert terms("Who was the man that she saw at the door?") == [
        "man",
        "saw",
        "door",
    ]


# Ranking -------------------------------------------------------------------


def test_the_paragraph_with_more_query_terms_ranks_first() -> None:
    text = book()

    hits = search_text(text, "snow train")

    assert hits[0]["index"] == 0
    assert hits[0]["text"] == PARAGRAPHS[0]
    assert [hit["score"] for hit in hits] == sorted(
        [hit["score"] for hit in hits], reverse=True
    )
    assert all(hit["score"] > 0 for hit in hits)


def test_a_rare_term_outranks_a_common_one() -> None:
    text = book()

    hits = search_text(text, "train detective", limit=5)

    # "detective" appears once, "train" twice, so the detective paragraph wins.
    assert hits[0]["index"] == 3


def test_each_hit_carries_its_paragraph_number_and_position() -> None:
    text = book()

    hit = search_text(text, "ratchett")[0]

    assert hit["index"] == 2
    assert hit["fraction"] == round(
        text["cum_chars"][2] / total_chars(text), 4
    )


def test_a_query_of_only_stop_words_finds_nothing() -> None:
    assert search_text(book(), "the of and it") == []


def test_a_word_that_is_not_in_the_book_finds_nothing() -> None:
    assert search_text(book(), "zeppelin") == []


# Limits and caps -----------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(1, 1), (2, 2), (5, 4), (99, 4), (0, 1), (-3, 1)],
)
def test_the_limit_is_clamped(limit: int, expected: int) -> None:
    hits = search_text(book(), "the train snow man door", limit=limit)

    assert len(hits) == expected


def test_a_non_integer_limit_uses_the_default() -> None:
    # "train" hits two paragraphs and the default limit is three.
    assert len(search_text(book(), "train", limit="2")) == 2


def test_each_text_is_capped_and_prefers_a_word_boundary() -> None:
    long_paragraph = " ".join(["snow"] * 40)

    hits = search_text(book([long_paragraph]), "snow", max_chars=20)

    assert hits
    assert len(hits[0]["text"]) <= 20
    assert not hits[0]["text"].endswith(" ")


def test_a_book_shorter_than_the_cap_is_returned_whole() -> None:
    assert search_text(book(), "ratchett", max_chars=1200)[0]["text"] == (
        PARAGRAPHS[2]
    )


# Scopes --------------------------------------------------------------------


def centre_of(text: dict, index: int) -> float:
    """Return the fraction of the character where paragraph ``index`` starts.

    A paragraph that starts at the offset is not ``before`` it, which is what
    the ``before`` scope asserts.
    """

    return text["cum_chars"][index] / total_chars(text)


def test_before_keeps_only_earlier_paragraphs() -> None:
    text = book()

    hits = search_text(
        text,
        "snow train",
        scope="before",
        fraction=centre_of(text, 3),
        limit=5,
    )

    # Paragraph 0 mentions the snow and the train; 4 sits after the position.
    assert [hit["index"] for hit in hits] == [0]


def test_before_never_returns_the_current_paragraph() -> None:
    text = book()

    hits = search_text(
        text, "compartment", scope="before", fraction=centre_of(text, 3)
    )

    assert hits == []


def test_all_keeps_the_whole_book() -> None:
    text = book()

    hits = search_text(
        text, "train", scope="all", fraction=centre_of(text, 1), limit=5
    )

    assert [hit["index"] for hit in hits] == [0, 4]


def test_around_keeps_a_window_near_the_position(monkeypatch) -> None:
    monkeypatch.setattr(search, "AROUND_CHARS", 20)
    text = book()

    hits = search_text(
        text,
        "detective compartment door",
        scope="around",
        fraction=centre_of(text, 3),
        limit=5,
    )

    assert [hit["index"] for hit in hits] == [3]


def test_around_excludes_a_distant_passage(monkeypatch) -> None:
    monkeypatch.setattr(search, "AROUND_CHARS", 20)
    text = book()

    hits = search_text(
        text, "snow", scope="around", fraction=centre_of(text, 3), limit=5
    )

    assert hits == []


@pytest.mark.parametrize("scope", ["everywhere", "", None, 7])
def test_an_unknown_scope_searches_the_whole_book(scope) -> None:
    text = book()

    hits = search_text(text, "train", scope=scope, fraction=0.0, limit=5)

    assert [hit["index"] for hit in hits] == [0, 4]


def test_a_missing_fraction_falls_back_to_the_whole_book() -> None:
    text = book()

    hits = search_text(text, "train", scope="before", fraction=None, limit=5)

    assert [hit["index"] for hit in hits] == [0, 4]


# Empty and malformed books -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        None,
        {},
        {"paragraphs": []},
        {"paragraphs": "not a list"},
        {"paragraphs": [1, 2]},
        {"paragraphs": ["ok"], "cum_chars": "bad"},
    ],
)
def test_a_book_without_usable_text_returns_nothing(text) -> None:
    assert search_text(text, "train") == []


def test_recomputed_offsets_handle_a_missing_cum_chars() -> None:
    hits = search_text({"paragraphs": PARAGRAPHS}, "ratchett")

    assert hits[0]["index"] == 2


def test_an_empty_query_returns_nothing() -> None:
    assert search_text(book(), "") == []
    assert search_text(book(), "   ") == []
    assert search_text(book(), None) == []


# Bound callable ------------------------------------------------------------


def test_book_search_binds_the_book_and_the_reading_position() -> None:
    text = book()
    search_fn = book_search(text, fraction=centre_of(text, 3))

    before = search_fn("train", scope="before", limit=5)
    everywhere = search_fn("train", scope="all", limit=5)

    assert [hit["index"] for hit in before] == [0]
    assert [hit["index"] for hit in everywhere] == [0, 4]


def test_book_search_never_raises_for_bad_arguments() -> None:
    search_fn = book_search(book())

    assert search_fn("train", scope="nowhere", limit=0)
    assert search_fn("", scope="all", limit=3) == []
    assert book_search(None)("train") == []


def test_book_search_caps_the_returned_text() -> None:
    search_fn = book_search(book([("snow " * 100).strip()]), max_chars=30)

    hits = search_fn("snow")

    assert hits
    assert len(hits[0]["text"]) <= 30
