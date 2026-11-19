"""Tests for the debug page, the trace feed, the dev ask and the sync routes.

Everything runs in process through FastAPI's ``TestClient`` and a pytest
``tmp_path`` directory. No network call is made: a dev ask without an OpenRouter
key records a skipped stage, and the pipeline tests inject their own hooks.
"""

from __future__ import annotations

import base64
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from voice_bridge_server._shell import escape_html
from voice_bridge_server.app import create_app, create_app_from_env
from voice_bridge_server.config import Settings
from voice_bridge_server.trace import log_stage
from voice_bridge_server.transcribe import DEFAULT_MODEL as DEFAULT_TRANSCRIBE_MODEL

try:  # pragma: no cover - branch depends on the pytest import mode
    from . import _wav_builders
except ImportError:  # pragma: no cover
    import _wav_builders

TOKEN = "test-token-0123456789abcdef"
MISSING_ID = "rec_20260101T000000Z_" + "a" * 32
BOOK_KEY = "bk_0123456789abcdef"


@pytest.fixture
def storage_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "recordings"


@pytest.fixture
def settings(storage_dir: pathlib.Path) -> Settings:
    return Settings(token=TOKEN, storage_dir=storage_dir)


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _wav(*, frames: int = 100) -> bytes:
    return _wav_builders.build_wav(frames=frames)


def _upload(client: TestClient, *, context: str | None = None) -> str:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "audio/wav",
    }
    if context is not None:
        headers["X-KVB-Context"] = context
    response = client.post("/v1/recordings", content=_wav(), headers=headers)
    assert response.status_code == 201
    return response.json()["id"]


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _context_header(payload: dict) -> str:
    body = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def _read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# Page ----------------------------------------------------------------------


def test_the_debug_page_is_served_without_auth(client: TestClient) -> None:
    response = client.get("/debug")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "content-security-policy" in response.headers


def test_the_debug_page_contains_the_questions_and_no_markup_writes(
    client: TestClient,
) -> None:
    text = client.get("/debug").text

    assert 'id="questions"' in text
    assert 'id="dev-form"' in text
    assert 'id="logs"' in text
    assert "/api/traces" in text
    assert "/v1/dev/ask" in text
    assert "/api/library/sync" in text
    assert "sessionStorage" in text
    assert "innerHTML" not in text
    assert "<script src" not in text
    assert "http://" not in text
    assert "https://" not in text


def test_the_debug_page_shows_the_summary_cards_and_the_sections(
    client: TestClient,
) -> None:
    text = client.get("/debug").text

    # The default view is one column of sections, summary first.
    assert "<h2>Questions</h2>" in text
    assert "<h2>Library</h2>" in text
    assert "<h2>Tools</h2>" in text
    assert text.index('id="summary-section"') < text.index('id="questions"')
    assert text.index('id="questions"') < text.index('id="library"')
    assert text.index('id="library"') < text.index('id="tools-section"')

    for card_id, label in (
        ("summary-books", "Books"),
        ("summary-questions", "Questions"),
        ("summary-answers", "Answers"),
        ("summary-errors", "Errors"),
        ("summary-last", "Last activity"),
    ):
        assert f'id="{card_id}"' in text
        assert f'>{label}</p>' in text


def test_a_question_row_carries_the_book_title_and_the_trace_link(
    client: TestClient,
) -> None:
    text = client.get("/debug").text

    # One compact row: time, book, page, kind, answer preview, outcome.
    for cell in ("q-time", "q-book", "q-page", "q-preview"):
        assert cell in text
    assert '"badge kind"' in text
    assert '"badge outcome"' in text
    # The book and the answer come from the summary and the trace it names.
    assert "item.book_title" in text
    assert "Unknown book" in text
    assert '"/api/traces/" + encodeURIComponent(id)' in text
    assert "parsed.highlight" in text


def test_a_book_row_carries_its_question_count(client: TestClient) -> None:
    text = client.get("/debug").text

    assert "book-meta" in text
    for label in ("Characters", "Questions", "Last asked", "Last synced"):
        assert label in text
    assert "bookCards" in text
    assert "updateBookCounts" in text
    # Counting is done on the loaded trace summaries, not in the database.
    assert "item.book_title" in text
    assert "Show all books" in text
    assert 'id="show-all-books"' in text


def test_the_tools_are_collapsed_by_default_but_still_wired(
    client: TestClient,
) -> None:
    text = client.get("/debug").text

    for tool, summary in (
        ("tools-dev", "Dev ask"),
        ("tools-sync", "Library sync"),
        ("tools-logs", "Log tail"),
        ("tools-config", "Config"),
    ):
        assert f'<details class="disclosure" id="{tool}">' in text
        assert f"<summary>{summary}</summary>" in text
    assert 'id="token-box"' in text
    assert 'id="dev-form"' in text
    assert 'id="sync"' in text
    assert 'id="dry-run"' in text
    assert 'id="log-filter"' in text
    assert 'id="logs"' in text
    assert "toolsDevEl.open = true" in text
    assert "toolsSyncEl.open = true" in text


def test_the_debug_page_keeps_the_trace_deep_link(client: TestClient) -> None:
    text = client.get("/debug").text

    assert "/debug#" in text
    assert 'id="pinned-section"' in text
    assert "hashchange" in text
    assert "openTrace" in text


def test_the_debug_page_writes_no_markup_from_data(client: TestClient) -> None:
    text = client.get("/debug").text

    assert "innerHTML" not in text
    assert "outerHTML" not in text
    assert "insertAdjacentHTML" not in text
    assert "document.write" not in text
    assert "textContent" in text


def test_the_debug_page_never_contains_the_token(
    client: TestClient, settings: Settings
) -> None:
    text = client.get("/debug").text

    assert TOKEN not in text
    assert str(settings.storage_dir) in text


def test_the_config_strip_escapes_its_values(tmp_path: pathlib.Path) -> None:
    library_dir = tmp_path / "lib & books"
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "recordings",
        library_dir=library_dir,
    )
    with TestClient(create_app(settings)) as client:
        text = client.get("/debug").text

    assert escape_html(str(library_dir)) in text
    assert "lib & books" not in text
    assert DEFAULT_TRANSCRIBE_MODEL in text


# Traces --------------------------------------------------------------------


def test_the_trace_feed_is_open_and_lists_recent_asks(
    client: TestClient,
) -> None:
    recording_id = _upload(client)

    response = client.get("/api/traces")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    summary = payload["traces"][0]
    assert summary["id"] == recording_id
    assert summary["origin"] == "kindle"
    assert summary["outcome"] == "ok"
    assert summary["stage"] == "context"


def test_the_trace_feed_rejects_an_unparseable_limit(client: TestClient) -> None:
    _upload(client)

    payload = client.get("/api/traces?limit=abc").json()

    assert payload["count"] == 1


def test_the_trace_detail_is_open_and_returns_the_whole_trace(
    client: TestClient, settings: Settings
) -> None:
    recording_id = _upload(client)

    response = client.get(f"/api/traces/{recording_id}")

    assert response.status_code == 200
    trace = response.json()
    assert trace == _read_json(
        settings.storage_dir / f"{recording_id}.trace.json"
    )
    assert trace["id"] == recording_id


@pytest.mark.parametrize(
    "recording_id",
    [
        MISSING_ID,
        "not-a-trace",
        "%2E%2E%2Fescape",
        "rec_20260101T000000Z_" + "b" * 32,
    ],
)
def test_a_forged_or_missing_trace_id_is_404(
    client: TestClient, recording_id: str
) -> None:
    response = client.get(f"/api/traces/{recording_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_a_trace_id_cannot_read_outside_the_storage_directory(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    (storage_dir.parent / "escape.trace.json").write_text(
        json.dumps({"id": "escape"}), encoding="utf-8"
    )

    response = client.get("/api/traces/%2E%2E%2Fescape")

    assert response.status_code == 404


def test_debug_text_off_hides_the_context_text_but_keeps_the_stages(
    tmp_path: pathlib.Path,
) -> None:
    settings = Settings(
        token=TOKEN, storage_dir=tmp_path / "recordings", debug_text=False
    )
    context = _context_header(
        {"v": 1, "book": {"title": "The Dispossessed"}, "highlight": "secret"}
    )

    with TestClient(create_app(settings)) as client:
        recording_id = _upload(client, context=context)

    trace = _read_json(
        settings.storage_dir / f"{recording_id}.trace.json"
    )
    assert trace["context"]["parsed"]["highlight"] == ""
    names = [stage["name"] for stage in trace["stages"]]
    assert names[:4] == ["receive", "validate", "store", "context"]
    assert "resolve" in names


# Logs ----------------------------------------------------------------------


def test_the_log_tail_is_open_and_filters_by_id(client: TestClient) -> None:
    # A unique id keeps this test independent of the other tests' log lines.
    marker = "rec_20260104T000000Z_" + "d" * 32
    log_stage("receive", marker, duration_ms=1.5)

    response = client.get(f"/api/logs?limit=5&q={marker}")

    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len(lines) == 1
    assert f"id={marker}" in lines[0]


def test_the_log_tail_returns_the_newest_lines(client: TestClient) -> None:
    marker = "rec_20260102T000000Z_" + "c" * 32
    for index in range(3):
        log_stage("answer", marker, attempts=index)

    payload = client.get(f"/api/logs?limit=2&q={marker}").json()

    assert len(payload["lines"]) == 2
    assert "attempts=2" in payload["lines"][-1]


# Library -------------------------------------------------------------------


def _write_book(library_dir: pathlib.Path, **fields: object) -> None:
    meta = {
        "key": BOOK_KEY,
        "title": "The Dispossessed",
        "authors": ["Ursula K. Le Guin"],
        "chars": 120,
    }
    meta.update(fields)
    directory = library_dir / BOOK_KEY
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_the_library_is_open_and_lists_books(tmp_path: pathlib.Path) -> None:
    library_dir = tmp_path / "library"
    _write_book(library_dir)
    settings = Settings(
        token=TOKEN, storage_dir=tmp_path / "recordings", library_dir=library_dir
    )

    with TestClient(create_app(settings)) as client:
        payload = client.get("/api/library").json()

    assert payload["dir"] == str(library_dir.resolve())
    assert payload["count"] == 1
    assert payload["books"][0]["title"] == "The Dispossessed"
    assert payload["last_sync"] is None


def test_a_sync_dry_run_returns_the_report_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "books"
    source.mkdir(parents=True)
    (source / "notes.txt").write_text("not a book", encoding="utf-8")
    store = tmp_path / "library"
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "recordings",
        library_dir=store,
        library_source=source,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/library/sync?dry_run=1", headers=_auth())
        traces = client.get("/api/traces").json()

    assert response.status_code == 200
    report = response.json()
    assert report["dry_run"] is True
    assert report["source_dir"] == str(source.resolve())
    assert [row["status"] for row in report["rows"]] == ["unsupported"]
    assert not (store / ".sync").exists()
    assert traces["count"] == 0


def test_a_sync_ignores_a_path_parameter(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "books"
    source.mkdir(parents=True)
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "recordings",
        library_dir=tmp_path / "library",
        library_source=source,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/library/sync?dry_run=1&path=/etc", headers=_auth()
        )

    assert response.status_code == 200
    assert response.json()["source_dir"] == str(source.resolve())


def test_a_sync_writes_a_report_but_no_trace(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "books"
    source.mkdir(parents=True)
    store = tmp_path / "library"
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "recordings",
        library_dir=store,
        library_source=source,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/library/sync", headers=_auth())
        traces = client.get("/api/traces").json()

    assert response.status_code == 200
    assert response.json()["dry_run"] is False
    assert list((store / ".sync").glob("*.json"))
    assert traces["count"] == 0


def _write_synced_book(
    library_dir: pathlib.Path,
    *,
    paragraphs: list[str],
    title: str = "The Dispossessed",
    authors: tuple[str, ...] = ("Ursula K. Le Guin",),
) -> str:
    """Write one complete book folder: ``meta.json`` and ``text.json``."""

    from voice_bridge_server.context import book_key

    key = book_key({"title": title, "authors": list(authors)})
    directory = library_dir / key
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "key": key,
        "title": title,
        "authors": list(authors),
        "aliases": [],
        "chars": sum(len(paragraph) for paragraph in paragraphs),
    }
    (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    offsets: list[int] = []
    total = 0
    for paragraph in paragraphs:
        offsets.append(total)
        total += len(paragraph)
    text = {"paragraphs": paragraphs, "cum_chars": offsets}
    (directory / "text.json").write_text(json.dumps(text), encoding="utf-8")
    return key


BOOK_PARAGRAPHS = [
    "She walked the dusty road down to the port.",
    "The ansible carried no message that year.",
    "He spoke of the thunderhead and the wall.",
]


def test_a_sync_without_a_source_is_rejected(tmp_path: pathlib.Path) -> None:
    settings = Settings(
        token=TOKEN, storage_dir=tmp_path / "recordings", library_dir=tmp_path / "lib"
    )

    with TestClient(create_app(settings)) as client:
        response = client.post("/api/library/sync", headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "library_source_missing"


def test_the_library_resolver_returns_a_fraction_passage() -> None:
    class FakeLibrary:
        def find(self, title, authors, *, size_bytes=None, partial_md5=None):
            return BOOK_KEY

        def load(self, key):
            return {
                "meta": {"aliases": []},
                "text": {"paragraphs": ["Alpha beta gamma."], "cum_chars": [0]},
            }

    from voice_bridge_server.app import _library_resolver

    passage, rung, reason, key, match, search = _library_resolver(FakeLibrary())(
        {"book": {"title": "T", "authors": []}, "pos": {"fraction": 0.5}}
    )

    assert passage == "Alpha beta gamma."
    assert rung == "fraction"
    assert key == BOOK_KEY
    assert match == "title+size"
    # The same loaded text becomes the search callable of the answer loop.
    assert callable(search)
    assert search("beta")[0]["index"] == 0
    assert search("delta") == []


# Auth ----------------------------------------------------------------------


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Basic abc", ""])
def test_the_dev_ask_requires_the_bearer_token(
    client: TestClient, auth: str | None
) -> None:
    headers = {} if auth is None else {"Authorization": auth}

    response = client.post(
        "/v1/dev/ask", json={"question": "Who wrote Dune?"}, headers=headers
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "www-authenticate" in response.headers
    assert not list(client.get("/api/traces").json()["traces"])


@pytest.mark.parametrize("auth", [None, "Bearer wrong"])
def test_the_sync_requires_the_bearer_token(
    client: TestClient, auth: str | None
) -> None:
    headers = {} if auth is None else {"Authorization": auth}

    response = client.post("/api/library/sync", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# Dev ask -------------------------------------------------------------------


def test_a_dev_ask_stores_a_recording_and_a_trace(
    client: TestClient, settings: Settings
) -> None:
    context = {"v": 1, "book": {"title": "The Dispossessed"}, "pos": {"page": 3}}

    response = client.post(
        "/v1/dev/ask",
        json={"question": "What is a thunderhead?", "context": context},
        headers=_auth(),
    )

    assert response.status_code == 201
    payload = response.json()
    recording_id = payload["id"]
    assert payload["trace"] == recording_id
    assert (settings.storage_dir / f"{recording_id}.wav").is_file()

    trace = _read_json(settings.storage_dir / f"{recording_id}.trace.json")
    assert trace["origin"] == "dev"
    assert trace["outcome"] == "ok"
    assert trace["context"]["parsed"] == context
    names = [stage["name"] for stage in trace["stages"]]
    assert names == ["store", "context", "resolve", "transcribe"]
    # A dev ask carries typed text, so the audio stage is recorded as skipped.
    stage = trace["stages"][-1]
    assert stage["outcome"] == "skipped"
    assert stage["detail"]["source"] == "dev-question"


def test_a_deleted_dev_ask_does_not_recreate_its_trace(
    client: TestClient, settings: Settings
) -> None:
    recording_id = "rec_20260101T000000Z_" + "b" * 32
    path = settings.storage_dir / f"{recording_id}.wav"

    client.app.state.pipeline(path, origin="dev", question="hello")

    assert not (settings.storage_dir / f"{recording_id}.trace.json").exists()


def test_a_dev_ask_without_a_key_is_not_an_error(client: TestClient) -> None:
    response = client.post(
        "/v1/dev/ask", json={"question": "Who wrote Dune?"}, headers=_auth()
    )

    assert response.status_code == 201
    recording_id = response.json()["id"]
    trace = client.get(f"/api/traces/{recording_id}").json()
    stage = next(
        item for item in trace["stages"] if item["name"] == "transcribe"
    )
    assert stage["outcome"] == "skipped"
    assert trace["outcome"] == "ok"


def test_a_dev_ask_shows_a_pending_trace_while_the_pipeline_runs(
    tmp_path: pathlib.Path,
) -> None:
    seen: dict[str, object] = {}

    def answer(path, transcript, trace=None, trace_store=None):
        seen["outcome"] = trace_store.read(trace)["outcome"]
        seen["transcript"] = transcript

    settings = Settings(token=TOKEN, storage_dir=tmp_path / "recordings")
    with TestClient(create_app(settings, answer=answer)) as client:
        response = client.post(
            "/v1/dev/ask", json={"question": "Who wrote Dune?"}, headers=_auth()
        )

    assert response.status_code == 201
    assert seen["outcome"] == "pending"
    assert seen["transcript"] == "Who wrote Dune?"


def test_a_dev_ask_uses_the_same_answer_hooks_and_overrides(
    tmp_path: pathlib.Path,
) -> None:
    calls: list[dict[str, object]] = []

    def transcribe(path, trace=None, trace_store=None):
        calls.append({"transcribe": path})
        return {"status": "ok", "text": "spoken question"}

    def answer(
        path,
        transcript,
        context_block=None,
        trace=None,
        trace_store=None,
        prompt=None,
        model=None,
    ):
        calls.append(
            {
                "path": path,
                "transcript": transcript,
                "context_block": context_block,
                "prompt": prompt,
                "model": model,
            }
        )

    settings = Settings(token=TOKEN, storage_dir=tmp_path / "recordings")
    with TestClient(
        create_app(settings, transcribe=transcribe, answer=answer)
    ) as client:
        response = client.post(
            "/v1/dev/ask",
            json={
                "question": "Who wrote Dune?",
                "prompt": "Answer in one word.",
                "model": "test/model",
                "context": {"v": 1, "book": {"title": "Dune"}},
            },
            headers=_auth(),
        )

    assert response.status_code == 201
    # The dev ask never runs the audio transcriber; the typed question is the
    # transcript and the answer hook is the same one an upload uses.
    assert len(calls) == 1
    call = calls[0]
    assert call["transcript"] == "Who wrote Dune?"
    assert call["prompt"] == "Answer in one word."
    assert call["model"] == "test/model"
    assert "Dune" in str(call["context_block"])
    assert pathlib.Path(call["path"]).exists()


def test_a_dev_ask_with_a_synced_book_passes_a_book_search(
    tmp_path: pathlib.Path,
) -> None:
    from voice_bridge_server.app import _library_resolver
    from voice_bridge_server.library import Library

    library_dir = tmp_path / "library"
    _write_synced_book(library_dir, paragraphs=BOOK_PARAGRAPHS)
    seen: dict[str, object] = {}

    def answer(path, transcript, book_search=None):
        seen["book_search"] = book_search

    settings = Settings(
        token=TOKEN, storage_dir=tmp_path / "recordings", library_dir=library_dir
    )
    app = create_app(
        settings,
        answer=answer,
        resolve=_library_resolver(Library(library_dir)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/dev/ask",
            json={
                "question": "What is the thunderhead?",
                "context": {
                    "v": 1,
                    "book": {
                        "title": "The Dispossessed",
                        "authors": ["Ursula K. Le Guin"],
                    },
                    "pos": {"page": 250, "fraction": 0.95},
                },
            },
            headers=_auth(),
        )

    assert response.status_code == 201
    search = seen["book_search"]
    assert callable(search)
    hits = search("thunderhead", scope="before")
    assert [hit["index"] for hit in hits] == [2]
    assert "thunderhead" in hits[0]["text"]


def test_a_dev_ask_without_a_synced_book_passes_no_book_search(
    tmp_path: pathlib.Path,
) -> None:
    from voice_bridge_server.app import _library_resolver
    from voice_bridge_server.library import Library

    library_dir = tmp_path / "library"
    _write_synced_book(library_dir, paragraphs=BOOK_PARAGRAPHS)
    seen: dict[str, object] = {}

    def answer(path, transcript, book_search=None):
        seen["book_search"] = book_search

    settings = Settings(
        token=TOKEN, storage_dir=tmp_path / "recordings", library_dir=library_dir
    )
    app = create_app(
        settings,
        answer=answer,
        resolve=_library_resolver(Library(library_dir)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/dev/ask",
            json={
                "question": "Who wrote this?",
                "context": {
                    "v": 1,
                    "book": {"title": "A Book That Was Never Synced"},
                    "pos": {"fraction": 0.5},
                },
            },
            headers=_auth(),
        )

    assert response.status_code == 201
    assert seen["book_search"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": 12},
        {"question": "x" * 2001},
        {"question": "ok", "context": "not an object"},
        {"question": "ok", "prompt": 3},
        {"question": "ok", "model": []},
    ],
)
def test_a_dev_ask_validates_its_body(
    client: TestClient, payload: dict
) -> None:
    response = client.post("/v1/dev/ask", json=payload, headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert not list(client.get("/api/traces").json()["traces"])


def test_a_dev_ask_rejects_a_body_that_is_not_json(client: TestClient) -> None:
    response = client.post(
        "/v1/dev/ask",
        content=b"not json",
        headers={**_auth(), "Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_a_dev_ask_accepts_a_two_thousand_character_question(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/dev/ask",
        json={"question": "x" * 2000},
        headers=_auth(),
    )

    assert response.status_code == 201


def test_a_dev_ask_applies_the_length_cap_after_trimming(
    client: TestClient,
) -> None:
    """Two trailing spaces do not make a 2000-character question too long."""
    response = client.post(
        "/v1/dev/ask",
        json={"question": "x" * 2000 + "  "},
        headers=_auth(),
    )

    assert response.status_code == 201


# Delete --------------------------------------------------------------------


def test_deleting_a_recording_removes_its_trace_and_context(
    client: TestClient, settings: Settings
) -> None:
    recording_id = _upload(client, context=_context_header({"v": 1}))
    for suffix in (".wav", ".context.json", ".trace.json"):
        assert (settings.storage_dir / f"{recording_id}{suffix}").is_file()

    response = client.delete(f"/api/recordings/{recording_id}")

    assert response.status_code == 200
    for suffix in (".wav", ".context.json", ".trace.json"):
        assert not (settings.storage_dir / f"{recording_id}{suffix}").exists()
    assert client.get(f"/api/traces/{recording_id}").status_code == 404


def test_the_review_page_links_each_recording_to_its_trace(
    client: TestClient, settings: Settings
) -> None:
    recording_id = _upload(client)

    item = client.get("/api/recordings").json()["recordings"][0]

    assert item["id"] == recording_id
    assert item["trace"] == {"origin": "kindle", "outcome": "ok"}
    page = client.get("/").text
    assert "/debug#" in page
    assert "fillChips" in page


def test_the_review_listing_reports_no_trace_when_there_is_none(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    (storage_dir / f"{MISSING_ID}.wav").write_bytes(_wav())

    item = client.get("/api/recordings").json()["recordings"][0]

    assert item["id"] == MISSING_ID
    assert item["trace"] is None


def test_a_dev_ask_is_marked_dev_in_the_review_listing(
    client: TestClient,
) -> None:
    response = client.post(
        "/v1/dev/ask", json={"question": "Who wrote Dune?"}, headers=_auth()
    )

    recording_id = response.json()["id"]
    item = client.get("/api/recordings").json()["recordings"][0]
    assert item["id"] == recording_id
    assert item["trace"]["origin"] == "dev"


# Configuration -------------------------------------------------------------


def test_a_trace_with_tool_calls_is_served_for_the_detail_panel(
    tmp_path: pathlib.Path,
) -> None:
    from voice_bridge_server.answer import DEFAULT_ANSWER_MODEL, SEARCH_TOOL_NAME
    from voice_bridge_server.answers import answer_recording
    from voice_bridge_server.trace import TraceStore

    settings = Settings(token=TOKEN, storage_dir=tmp_path / "recordings")
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    store = TraceStore(settings.storage_dir, raw=True)
    store.start(MISSING_ID, origin="dev")
    bodies: list[dict] = []

    def transport(url, headers, payload, timeout):
        bodies.append(json.loads(payload))
        if len(bodies) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {
                            "name": SEARCH_TOOL_NAME,
                            "arguments": json.dumps(
                                {"query": "Ratchett", "scope": "before"}
                            ),
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "He was next door."}
        return 200, json.dumps(
            {
                "model": DEFAULT_ANSWER_MODEL,
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.0002},
            }
        ).encode("utf-8")

    wav = settings.storage_dir / f"{MISSING_ID}.wav"
    wav.write_bytes(_wav())
    answer_recording(
        wav,
        "Who is Ratchett?",
        "secret-key",
        transport=transport,
        book_search=lambda query, scope="all", limit=3: [
            {"text": "Ratchett was afraid.", "index": 2, "fraction": 0.3, "score": 2.0}
        ],
        trace=MISSING_ID,
        trace_store=store,
    )

    with TestClient(create_app(settings)) as client:
        trace = client.get(f"/api/traces/{MISSING_ID}").json()

    assert trace["llm"]["rounds"] == 2
    tools = trace["llm"]["tools"]
    assert len(tools) == 1
    assert tools[0]["round"] == 1
    assert tools[0]["name"] == SEARCH_TOOL_NAME
    assert tools[0]["arguments"] == {"query": "Ratchett", "scope": "before"}
    assert tools[0]["hits"] == 1
    assert trace["llm"]["cost_usd"] == pytest.approx(0.0004)
    assert [stage["name"] for stage in trace["stages"]] == [
        "prompt",
        "answer",
        "sidecars",
    ]


def test_the_detail_panel_renders_the_book_searches_block(
    client: TestClient,
) -> None:
    text = client.get("/debug").text

    assert "Book searches" in text
    assert "bookSearchTable" in text
    assert "llm.tools" in text
    # Only the query, the scope, the hit count and the duration are shown.
    assert "arguments_.query" in text
    assert "tools_unsupported" in text


def test_create_app_from_env_defaults_the_library_under_storage(
    tmp_path: pathlib.Path,
) -> None:
    storage = tmp_path / "recordings"

    app = create_app_from_env(
        {"KVB_TOKEN": TOKEN, "KVB_STORAGE_DIR": str(storage)}
    )

    assert app.state.settings.library_dir == (storage / "library").resolve()
    assert app.state.library.root == (storage / "library").resolve()
    assert app.state.settings.debug_text is True
    assert app.state.settings.log_level == "INFO"


def test_create_app_from_env_reads_the_debug_variables(
    tmp_path: pathlib.Path,
) -> None:
    library = tmp_path / "books"

    app = create_app_from_env(
        {
            "KVB_TOKEN": TOKEN,
            "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
            "KVB_LIBRARY_DIR": str(library),
            "KVB_DEBUG_TEXT": "0",
            "KVB_LOG_LEVEL": "warning",
        }
    )

    assert app.state.settings.library_dir == library.resolve()
    assert app.state.library.root == library.resolve()
    assert app.state.settings.debug_text is False
    assert app.state.settings.log_level == "WARNING"


def test_settings_rejects_an_invalid_log_level_type(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            token=TOKEN, storage_dir=tmp_path / "recordings", log_level=""
        )
