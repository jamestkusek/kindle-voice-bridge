"""Book search over one synced book, for the answer model's search tool.

The answer model may ask for text that is not on the current page: an earlier
chapter, a recurring character, why the page is in this state. This module
serves that request from the text that
:class:`voice_bridge_server.library.Library` already extracted, so a search
costs no disk read and no new dependency.

Only the standard library is used. Ranking is BM25 over paragraphs, with the
usual ``k1`` and ``b``. A paragraph is the unit of return: the EPUB extractor
already split the book into paragraphs, and ``cum_chars`` maps each one back to
a character offset in the book, which is what ``fraction`` is measured in.

Every function is pure. ``text`` is the payload from ``Library.load(key)``::

    {"paragraphs": ["..."], "cum_chars": [0, 120, 300]}

:func:`search_text` returns at most ``limit`` results, best first, or ``[]``.
:func:`book_search` binds one book and one reading position into the callable
that the answer loop invokes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

#: BM25 term-frequency saturation and length normalisation.
BM25_K1 = 1.5
BM25_B = 0.75

#: Scopes a search may use. An unknown scope is treated as ``all``.
SCOPES = ("before", "around", "all")

#: Characters on either side of the current offset that ``around`` keeps.
AROUND_CHARS = 20000

#: Longest text one result carries by default.
DEFAULT_MAX_CHARS = 1200

#: Results one search returns by default, and the most it will return.
DEFAULT_LIMIT = 3
MAX_LIMIT = 5

#: A word is a run of lower-case ASCII letters and digits, like the book keys.
_WORD = re.compile(r"[a-z0-9]+")

#: Small English stop-word set. Common words carry no book meaning and would
#: match every paragraph.
STOP_WORDS = frozenset(
    {
        "a", "about", "after", "again", "all", "also", "am", "an", "and",
        "any", "are", "as", "at", "be", "because", "been", "before", "being",
        "but",
        "by", "can", "could", "did", "do", "does", "doing", "for", "from",
        "had", "has", "have", "having", "he", "her", "here", "hers", "him",
        "his", "how", "i", "if", "in", "into", "is", "it", "its", "just",
        "me", "more", "most", "my", "no", "not", "now", "of", "on", "once",
        "only", "or", "other", "our", "out", "over", "said", "same", "she",
        "should", "so", "some", "such", "than", "that", "the", "their",
        "them", "then", "there", "these", "they", "this", "those", "through",
        "to", "too", "under", "up", "very", "was", "we", "were", "what",
        "when", "where", "which", "while", "who", "whom", "why", "will",
        "with", "would", "you", "your",
    }
)


def tokenise(value: object) -> list[str]:
    """Return the lower-case alphanumeric words of ``value``."""

    if not isinstance(value, str):
        return []
    return _WORD.findall(value.lower())


def terms(value: object) -> list[str]:
    """Return the searchable words of ``value``, stop words removed."""

    return [word for word in tokenise(value) if word not in STOP_WORDS]


def search_text(
    text: object,
    query: object,
    *,
    scope: str = "all",
    fraction: float | None = None,
    limit: int = DEFAULT_LIMIT,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Rank the paragraphs of one book against ``query``.

    ``scope`` filters the corpus before ranking: ``before`` keeps paragraphs
    that start before the current character offset, ``around`` keeps a window
    of :data:`AROUND_CHARS` on either side, and ``all`` keeps the whole book.
    The offset is ``fraction`` of the book length, so a missing or unusable
    ``fraction`` falls back to ``all``.

    Each result is ``{"text", "index", "fraction", "score"}``, where ``index``
    is the paragraph number in the whole book and ``fraction`` is its position.
    Results are ordered by score, highest first, then by paragraph order. The
    list is empty when there is no book, no query or no hit.
    """

    paragraphs, offsets = _book(text)
    if not paragraphs:
        return []
    wanted = terms(query)
    if not wanted:
        return []

    total = offsets[-1] + len(paragraphs[-1])
    centre = _offset(fraction, total)
    selected = _scope(paragraphs, offsets, total, scope, centre)
    if not selected:
        return []

    documents = [(index, terms(paragraphs[index])) for index in selected]
    scores = _bm25(documents, wanted)
    hits = [
        (index, score) for index, score in scores.items() if score > 0.0
    ]
    if not hits:
        return []
    hits.sort(key=lambda hit: (-hit[1], hit[0]))

    cap = _cap_chars(max_chars)
    kept = hits[: _count(limit)]
    return [
        {
            "text": _trim(paragraphs[index], cap),
            "index": index,
            "fraction": round(offsets[index] / total, 4) if total else 0.0,
            "score": round(score, 4),
        }
        for index, score in kept
    ]


def book_search(
    text: object,
    *,
    fraction: float | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Callable[..., list[dict[str, Any]]]:
    """Bind one loaded book to a search callable for the answer loop.

    The returned callable takes ``(query, scope="all", limit=3)`` and searches
    this book at this reading position. It never raises for bad input: the
    limits are clamped and an unknown scope searches the whole book.
    """

    book = text if isinstance(text, dict) else {}

    def search(
        query: object, scope: str = "all", limit: int = DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        return search_text(
            book,
            query,
            scope=scope,
            fraction=fraction,
            limit=limit,
            max_chars=max_chars,
        )

    return search


def _book(text: object) -> tuple[list[str], list[int]]:
    """Return ``(paragraphs, cum_chars)`` from a ``text.json`` payload.

    A missing or inconsistent ``cum_chars`` is recomputed from the paragraphs,
    so a caller cannot pass a shape that breaks the offset arithmetic.
    """

    if not isinstance(text, dict):
        return [], []
    paragraphs = text.get("paragraphs")
    if not isinstance(paragraphs, list):
        return [], []
    texts = [item for item in paragraphs if isinstance(item, str)]
    if len(texts) != len(paragraphs):
        return [], []

    offsets = text.get("cum_chars")
    if isinstance(offsets, list) and len(offsets) == len(texts):
        numbers = [
            item
            for item in offsets
            if isinstance(item, int) and not isinstance(item, bool)
            and item >= 0
        ]
        if len(numbers) == len(texts) and all(
            later >= earlier for earlier, later in zip(numbers, numbers[1:])
        ):
            return texts, numbers
    return texts, _cumulative(texts)


def _cumulative(paragraphs: list[str]) -> list[int]:
    """Return the start offset of each paragraph in the joined text."""

    offsets: list[int] = []
    total = 0
    for paragraph in paragraphs:
        offsets.append(total)
        total += len(paragraph)
    return offsets


def _offset(fraction: object, total: int) -> int | None:
    """Return the current character offset, or ``None`` when it is unknown."""

    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return None
    if not math.isfinite(fraction) or total <= 0:
        return None
    clamped = min(1.0, max(0.0, float(fraction)))
    return min(total, int(clamped * total))


def _scope(
    paragraphs: list[str],
    offsets: list[int],
    total: int,
    scope: object,
    centre: int | None,
) -> list[int]:
    """Return the paragraph numbers that ``scope`` keeps."""

    name = scope if isinstance(scope, str) else "all"
    if name not in SCOPES or centre is None:
        return list(range(len(paragraphs)))
    if name == "before":
        return [index for index, start in enumerate(offsets) if start < centre]
    low = max(0, centre - AROUND_CHARS)
    high = min(total, centre + AROUND_CHARS)
    return [
        index
        for index, start in enumerate(offsets)
        if start + len(paragraphs[index]) > low and start < high
    ]


def _bm25(
    documents: list[tuple[int, list[str]]], query: list[str]
) -> dict[int, float]:
    """Return the BM25 score of every document that matches the query."""

    if not documents:
        return {}
    lengths = [len(words) for _, words in documents]
    average = sum(lengths) / len(lengths) if lengths else 0.0
    wanted = list(dict.fromkeys(query))

    frequency: dict[str, int] = {}
    for _, words in documents:
        for word in wanted:
            if word in words:
                frequency[word] = frequency.get(word, 0) + 1
    idf = {
        word: math.log(
            1.0 + (len(documents) - count + 0.5) / (count + 0.5)
        )
        for word, count in frequency.items()
        if count
    }

    scores: dict[int, float] = {}
    for (index, words), length in zip(documents, lengths):
        if not words:
            continue
        score = 0.0
        for word, weight in idf.items():
            count = words.count(word)
            if not count:
                continue
            norm = BM25_K1 * (1.0 - BM25_B + BM25_B * length / average)
            score += weight * count * (BM25_K1 + 1.0) / (count + norm)
        if score:
            scores[index] = score
    return scores


def _count(limit: object) -> int:
    """Clamp ``limit`` to 1..:data:`MAX_LIMIT`."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        return DEFAULT_LIMIT
    return min(MAX_LIMIT, max(1, limit))


def _cap_chars(max_chars: object) -> int:
    """Clamp ``max_chars`` to a positive integer."""

    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        return DEFAULT_MAX_CHARS
    return max_chars if max_chars > 0 else DEFAULT_MAX_CHARS


def _trim(text: str, max_chars: int) -> str:
    """Cap one paragraph, preferring a word boundary."""

    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    space = clipped.rfind(" ")
    if space >= max_chars // 2:
        clipped = clipped[:space]
    return clipped.rstrip()
