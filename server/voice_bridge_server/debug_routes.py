"""Debug page, trace feed, dev ask and library sync.

These routes are a local development aid for the observability bet in
``docs/plans/milestone-2-context-observability.md``: one trace file per ask is
the live feed, the detail page and the prompt record. They are outside the
upload contract in ``docs/protocol.md``.

The page is unauthenticated and must only be reachable on a trusted LAN. It
never receives the bearer token from the server: the operator types the token
into the page and the browser keeps it in ``sessionStorage``. Only the dev ask
and the sync endpoint check it, and the token is never logged.

Every value the page renders goes through a DOM text node. The server writes
only the config strip into the document, escaped with
:func:`voice_bridge_server._shell.escape_html`; no data value is turned into
markup anywhere.

A dev ask stores a 0.1 s silent WAV through the same recording store as an
upload and runs the same pipeline closure, so the trace, the stage timings and
the answer sidecars are produced by the code the receiver uses. The typed
question replaces the transcript of that silent clip: the pipeline records a
skipped ``transcribe`` stage and answers the question directly. A missing
OpenRouter key is not an error; the trace records a skipped stage.
"""

from __future__ import annotations

import pathlib
import struct
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ._shell import escape_html, page_headers, page_shell
from .answer import DEFAULT_ANSWER_MODEL
from .config import Settings, token_matches
from .library import Library
from .storage import LocalFileStore, RecordingStore, new_recording_id
from .trace import LOG_BUFFER, LOG_BUFFER_SIZE, TraceStore
from .transcribe import DEFAULT_MODEL as DEFAULT_TRANSCRIBE_MODEL
from .transcripts import trace_safely

#: Longest accepted dev question, in characters.
MAX_QUESTION_CHARS = 2000

#: Longest accepted system prompt override, in characters.
MAX_PROMPT_CHARS = 4000

#: Longest accepted model override, in characters.
MAX_MODEL_CHARS = 200

#: Origin recorded for an ask that the debug page started.
DEV_ORIGIN = "dev"

#: Most trace summaries one feed poll returns.
DEFAULT_TRACE_LIMIT = 50
MAX_TRACE_LIMIT = 100

#: Most log lines one poll returns.
DEFAULT_LOG_LIMIT = 200

#: Silence a dev ask stores: 0.1 s of 8 kHz mono 16-bit PCM.
DEV_WAV_RATE_HZ = 8000
DEV_WAV_BITS = 16
DEV_WAV_FRAMES = 800

#: Query values that ask for a dry run.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Placeholder the page body uses for the server-rendered config strip.
_CONFIG_MARKER = "<!--config-->"


def silent_wav(
    *,
    frames: int = DEV_WAV_FRAMES,
    rate: int = DEV_WAV_RATE_HZ,
    bits: int = DEV_WAV_BITS,
) -> bytes:
    """Return a mono PCM WAV of ``frames`` silent samples.

    The header is built by hand so the module needs no test helper and no audio
    dependency. The format is the one ``POST /v1/recordings`` accepts.
    """

    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise ValueError("frames must be a positive integer")
    if isinstance(rate, bool) or not isinstance(rate, int) or rate <= 0:
        raise ValueError("rate must be a positive integer")
    if bits not in (8, 16):
        raise ValueError("bits must be 8 or 16")

    block_align = 1 if bits == 8 else 2
    data = b"\x00" * (frames * block_align)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * block_align,
        block_align,
        bits,
        b"data",
        len(data),
    )
    return header + data


def register_debug_routes(
    app: FastAPI,
    settings: Settings,
    trace_store: TraceStore,
    run_pipeline: Callable[..., None],
) -> None:
    """Attach the debug page and its JSON, dev-ask and library routes.

    ``run_pipeline`` is the app's pipeline closure. It receives the stored
    ``path`` and the ``origin``, ``context``, ``question``, ``prompt`` and
    ``model`` keywords, and runs as a background task so the request returns
    immediately.
    """

    store: RecordingStore | None = getattr(app.state, "store", None)
    if store is None:
        store = LocalFileStore(settings.storage_dir)
    library: Library | None = getattr(app.state, "library", None)
    if library is None:
        library = Library(_library_root(settings))

    @app.get("/debug", include_in_schema=False)
    def debug_page() -> HTMLResponse:
        return HTMLResponse(
            page_shell(
                "Kindle Voice Bridge debug",
                _render_body(settings, app, library),
                head=_PAGE_HEAD,
            ),
            headers=page_headers(),
        )

    @app.get("/api/traces", include_in_schema=False)
    def traces_api(request: Request) -> JSONResponse:
        limit = _query_int(
            request, "limit", DEFAULT_TRACE_LIMIT, 1, MAX_TRACE_LIMIT
        )
        summaries = trace_store.list_recent(limit)
        return _json_response({"count": len(summaries), "traces": summaries})

    @app.get("/api/traces/{recording_id}", include_in_schema=False)
    def trace_api(recording_id: str) -> JSONResponse:
        trace = _read_trace(trace_store, recording_id)
        if trace is None:
            return _error_response(404, "not_found", "Trace not found.")
        return _json_response(trace)

    @app.get("/api/logs", include_in_schema=False)
    def logs_api(request: Request) -> JSONResponse:
        limit = _query_int(request, "limit", DEFAULT_LOG_LIMIT, 1, LOG_BUFFER_SIZE)
        needle = (request.query_params.get("q") or "").strip().casefold()
        lines = [
            line
            for line in LOG_BUFFER
            if not needle or needle in line.casefold()
        ][-limit:]
        return _json_response({"count": len(lines), "lines": lines})

    @app.get("/api/library", include_in_schema=False)
    def library_api() -> JSONResponse:
        books = library.list_books()
        return _json_response(
            {
                "dir": str(library.root),
                "count": len(books),
                "books": books,
                "last_sync": library.last_report(),
            }
        )

    @app.post("/v1/dev/ask", include_in_schema=False)
    async def dev_ask(
        request: Request, background: BackgroundTasks
    ) -> Response:
        if not _authorized(request, settings.token):
            return _unauthorized()

        payload = await _json_body(request)
        if payload is None:
            return _error_response(
                400, "invalid_request", "The request body must be a JSON object."
            )

        question = payload.get("question")
        if not isinstance(question, str):
            return _error_response(
                400, "invalid_request", "question must be a non-empty string."
            )
        # The documented cap applies to the question that is sent, so trim
        # before the length check.
        question = question.strip()
        if not question:
            return _error_response(
                400, "invalid_request", "question must be a non-empty string."
            )
        if len(question) > MAX_QUESTION_CHARS:
            return _error_response(
                400,
                "invalid_request",
                f"question must be at most {MAX_QUESTION_CHARS} characters.",
            )

        context = payload.get("context")
        if context is not None and not isinstance(context, dict):
            return _error_response(
                400, "invalid_request", "context must be an object."
            )

        prompt, error = _optional_text(
            payload, "prompt", MAX_PROMPT_CHARS
        )
        if error is not None:
            return _error_response(400, "invalid_request", error)
        model, error = _optional_text(payload, "model", MAX_MODEL_CHARS)
        if error is not None:
            return _error_response(400, "invalid_request", error)

        recording_id = new_recording_id()
        body = silent_wav()
        try:
            path = store.save(recording_id, body)
        except OSError:
            return _error_response(
                500, "storage_error", "Could not store the dev recording."
            )

        # The trace header is written before the response, so the feed shows the
        # ask while the pipeline is still running. The question travels with the
        # pipeline because the stored clip is silent.
        _start_dev_trace(trace_store, recording_id, context, len(body))
        background.add_task(
            run_pipeline,
            path,
            origin=DEV_ORIGIN,
            context=context,
            question=question,
            prompt=prompt,
            model=model,
        )
        return _json_response(
            {"id": recording_id, "trace": recording_id}, 201
        )

    @app.post("/api/library/sync", include_in_schema=False)
    def sync_api(request: Request) -> JSONResponse:
        if not _authorized(request, settings.token):
            return _unauthorized()

        # No path is accepted: the sync only scans the configured source.
        dry_run = (
            request.query_params.get("dry_run", "").strip().lower()
            in _TRUE_VALUES
        )
        source = settings.library_source
        if source is None:
            return _error_response(
                400,
                "library_source_missing",
                "Set KVB_LIBRARY_SOURCE to the folder that holds the EPUB files.",
            )
        try:
            report = library.sync(source, dry_run=dry_run)
        except OSError:
            return _error_response(
                500, "library_error", "The library sync failed."
            )
        return _json_response(report)


# Request helpers -----------------------------------------------------------


def _authorized(request: Request, expected: str) -> bool:
    """Return True when the request carries exactly one matching bearer token."""

    values = request.headers.getlist("authorization")
    return len(values) == 1 and token_matches(values[0], expected)


def _unauthorized() -> JSONResponse:
    return _error_response(
        401,
        "unauthorized",
        "Missing or invalid bearer token.",
        extra_headers={"WWW-Authenticate": 'Bearer realm="voice-bridge"'},
    )


async def _json_body(request: Request) -> dict[str, Any] | None:
    """Return the request body as a JSON object, or ``None`` when it is not one."""

    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _optional_text(
    payload: dict[str, Any], name: str, limit: int
) -> tuple[str | None, str | None]:
    """Return one optional string field as ``(value, error)``."""

    value = payload.get(name)
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"{name} must be a string."
    if not value.strip():
        return None, None
    if len(value) > limit:
        return None, f"{name} must be at most {limit} characters."
    return value, None


def _query_int(
    request: Request, name: str, default: int, minimum: int, maximum: int
) -> int:
    """Return one bounded integer query parameter, or ``default``."""

    raw_value = request.query_params.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value.strip(), 10)
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


# Trace helpers -------------------------------------------------------------


def _read_trace(
    trace_store: TraceStore, recording_id: str
) -> dict[str, Any] | None:
    """Return one trace, or ``None`` for a forged, unsafe or unknown id."""

    try:
        return trace_store.read(recording_id)
    except (ValueError, OSError):
        return None


def _start_dev_trace(
    trace_store: TraceStore,
    recording_id: str,
    context: dict[str, Any] | None,
    body_bytes: int,
) -> None:
    """Write the trace header and the synthetic stages of one dev ask."""

    trace_safely(
        lambda: trace_store.start(
            recording_id,
            origin=DEV_ORIGIN,
            context={
                "header_bytes": 0,
                "parsed": context if context is not None else {},
                "rejected_reason": None,
            },
        )
    )
    trace_safely(
        lambda: trace_store.stage(
            recording_id,
            "store",
            detail={"bytes": body_bytes, "source": "dev"},
        )
    )
    trace_safely(
        lambda: trace_store.stage(
            recording_id,
            "context",
            detail={"header_bytes": 0, "source": "dev"},
        )
    )


# Page ----------------------------------------------------------------------


def _library_root(settings: Settings) -> pathlib.Path:
    """Return the library root, defaulting under the storage directory."""

    if settings.library_dir is not None:
        return settings.library_dir
    return settings.storage_dir / "library"


def _render_body(settings: Settings, app: FastAPI, library: Library) -> str:
    """Return the debug page body with the config strip filled in."""

    return _PAGE_BODY.replace(_CONFIG_MARKER, _config_strip(settings, app, library))


def _config_strip(settings: Settings, app: FastAPI, library: Library) -> str:
    """Return the server-rendered config strip as escaped HTML."""

    key_present = (
        getattr(app.state, "transcribe", None) is not None
        or getattr(app.state, "answer", None) is not None
    )
    rows = (
        ("Storage", str(settings.storage_dir)),
        ("Library", str(library.root)),
        (
            "Library source",
            str(settings.library_source)
            if settings.library_source is not None
            else "not set",
        ),
        ("Transcribe model", f"{DEFAULT_TRANSCRIBE_MODEL} (default)"),
        ("Answer model", f"{DEFAULT_ANSWER_MODEL} (default)"),
        ("OpenRouter key", "configured" if key_present else "missing"),
        ("Log level", settings.log_level),
        ("Trace text", "shown" if settings.debug_text else "hidden"),
    )
    cells = "".join(
        f"<div><dt>{escape_html(name)}</dt><dd>{escape_html(value)}</dd></div>"
        for name, value in rows
    )
    return f'<dl class="config">{cells}</dl>'


def _json_response(
    payload: dict[str, Any], status_code: int = 200
) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )


_PAGE_HEAD = """<style>
:root {
  color-scheme: light dark;
  --bg: #f5f6f8;
  --fg: #1b1f24;
  --muted: #5b6572;
  --card: #ffffff;
  --border: #d9dee5;
  --accent: #0b6bcb;
  --danger: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a;
    --fg: #e8eaed;
    --muted: #9aa4af;
    --card: #1c2024;
    --border: #2c3238;
    --accent: #6cb6ff;
    --danger: #ff8a80;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.25rem;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--fg);
}
header { max-width: 1100px; margin: 0 auto 1rem; display: grid; gap: 0.35rem; }
.title-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.75rem; }
h1 { font-size: 1.35rem; margin: 0; }
h2 {
  font-size: 0.8rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 0.6rem;
}
h3 { font-size: 0.9rem; margin: 0 0 0.5rem; }
h4 { font-size: 0.78rem; margin: 0.75rem 0 0.2rem; color: var(--muted); }
main { max-width: 1100px; margin: 0 auto; display: grid; gap: 1rem; }
section {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem;
}
.status { margin: 0; color: var(--muted); font-size: 0.85rem; }
.small { font-size: 0.8rem; }
#updated { margin-left: auto; color: var(--muted); font-size: 0.8rem; }
#updated.stale { color: var(--danger); }
button, input, textarea { font: inherit; color: var(--fg); }
button {
  padding: 0.35rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: 0.6; cursor: default; }
input, textarea {
  width: 100%;
  padding: 0.3rem 0.45rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
}
textarea { resize: vertical; }
label { display: block; font-size: 0.8rem; color: var(--muted); }
form .field { margin-bottom: 0.6rem; }
.row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem; margin-bottom: 0.6rem; }
.row label { display: flex; align-items: center; gap: 0.3rem; }
.row input[type="checkbox"] { width: auto; }
#status, #dev-status, #sync-status { color: var(--muted); font-size: 0.85rem; }
details.disclosure {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.5rem 0.7rem;
}
details.disclosure summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; }
details.disclosure summary:hover { color: var(--accent); }
details.disclosure[open] > summary { margin-bottom: 0.7rem; }
#tools-section details.disclosure + details.disclosure { margin-top: 0.4rem; }
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 0.6rem;
}
.card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.5rem 0.7rem;
  background: var(--bg);
}
.card-label {
  margin: 0;
  color: var(--muted);
  font-size: 0.72rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.card-value {
  margin: 0.1rem 0 0;
  font-size: 1.25rem;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.card-value.small { font-size: 0.95rem; }
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.6rem;
}
.section-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.6rem;
}
.section-head h2 { margin: 0; }
.section-head button { padding: 0.15rem 0.6rem; font-size: 0.8rem; }
#questions { display: grid; gap: 0.4rem; }
.question { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.question.stuck { border-color: var(--danger); }
.q-head {
  display: grid;
  grid-template-columns: 5.5rem minmax(7rem, 1.3fr) 3.5rem 5rem minmax(8rem, 2fr) 5.5rem;
  gap: 0.5rem;
  align-items: center;
  width: 100%;
  padding: 0.4rem 0.6rem;
  cursor: pointer;
  font-size: 0.82rem;
}
.q-head:hover { background: var(--bg); }
.q-head:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.question.open .q-head { border-bottom: 1px solid var(--border); }
.q-time, .q-page { color: var(--muted); font-variant-numeric: tabular-nums; }
.q-book {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  text-align: left;
  overflow-wrap: anywhere;
}
.q-book:hover { text-decoration: underline; }
.q-preview {
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.badge {
  display: inline-block;
  padding: 0 0.4rem;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--muted);
  font-size: 0.7rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  white-space: nowrap;
}
.badge.kind-highlight { border-color: var(--accent); color: var(--accent); }
.badge.outcome-ok { border-color: var(--accent); color: var(--accent); }
.badge.outcome-failed, .badge.outcome-rejected { border-color: var(--danger); color: var(--danger); }
.badge.outcome-pending { border-style: dashed; }
.panel { padding: 0.6rem; }
.books { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 0.6rem; }
.book { border: 1px solid var(--border); border-radius: 8px; padding: 0.6rem 0.7rem; }
.book.selected { border-color: var(--accent); }
.book-head {
  display: block;
  width: 100%;
  padding: 0;
  border: 0;
  background: none;
  text-align: left;
}
.book-title { font-weight: 600; color: var(--accent); overflow-wrap: anywhere; }
.book-head:hover .book-title { text-decoration: underline; }
.book-authors { display: block; color: var(--muted); font-size: 0.8rem; }
.book-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.25rem 0.6rem;
  margin: 0.5rem 0 0;
  font-size: 0.78rem;
}
.book-meta dt { color: var(--muted); }
.book-meta dd { margin: 0; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.book-detail { margin-top: 0.5rem; border-top: 1px solid var(--border); padding-top: 0.4rem; }
.tool + .tool { margin-top: 0.9rem; border-top: 1px solid var(--border); padding-top: 0.7rem; }
.config {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.5rem 1rem;
  margin: 0;
}
.config dt { color: var(--muted); font-size: 0.75rem; }
.config dd {
  margin: 0;
  font: 0.85rem ui-monospace, "Cascadia Mono", monospace;
  overflow-wrap: anywhere;
}
dl.meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.1rem 0.75rem;
  margin: 0.25rem 0;
  font-size: 0.82rem;
}
dl.meta dt { color: var(--muted); }
dl.meta dd { margin: 0; overflow-wrap: anywhere; }
pre.block {
  margin: 0.2rem 0 0;
  padding: 0.5rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  font: 0.8rem/1.45 ui-monospace, "Cascadia Mono", monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 16rem;
  overflow: auto;
}
table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
th, td {
  padding: 0.25rem 0.4rem;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}
th { color: var(--muted); font-weight: 600; }
td.mono { font-family: ui-monospace, "Cascadia Mono", monospace; }
.scroll { overflow-x: auto; }
.muted { color: var(--muted); }
.warn { color: var(--danger); font-size: 0.85rem; }
#logs {
  margin: 0.5rem 0 0;
  padding: 0.5rem;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  font: 0.75rem/1.5 ui-monospace, "Cascadia Mono", monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 20rem;
  overflow: auto;
}
#token { max-width: 22rem; }
#dev-question, #dev-prompt, #dev-model { max-width: 40rem; }
#log-filter { max-width: 20rem; }
@media (max-width: 900px) {
  body { padding: 1rem; }
  main { gap: 0.75rem; }
  section { padding: 0.8rem; }
  .q-head { display: flex; flex-wrap: wrap; gap: 0.3rem 0.6rem; }
  .q-preview { flex-basis: 100%; white-space: normal; }
  .books { grid-template-columns: 1fr; }
}
</style>"""


_PAGE_BODY = """<header>
  <div class="title-row">
    <h1>Kindle Voice Bridge</h1>
    <a class="muted" href="/">Recordings</a>
    <span id="updated" class="muted"></span>
  </div>
  <p id="status" class="status" role="status" aria-live="polite">Receiver status unknown.</p>
  <details class="disclosure" id="token-box">
    <summary>Bearer token</summary>
    <div class="row">
      <label for="token">Token</label>
      <input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="paste the KVB_TOKEN value">
      <button id="save-token" type="button">Save token</button>
    </div>
    <p class="muted small">Kept in this tab only. Only the dev ask and the sync send it.</p>
  </details>
</header>
<main>
  <section id="summary-section" aria-label="Summary">
    <div class="cards">
      <div class="card"><p class="card-label">Books</p><p class="card-value" id="summary-books">-</p></div>
      <div class="card"><p class="card-label">Questions</p><p class="card-value" id="summary-questions">-</p></div>
      <div class="card"><p class="card-label">Answers</p><p class="card-value" id="summary-answers">-</p></div>
      <div class="card"><p class="card-label">Errors</p><p class="card-value" id="summary-errors">-</p></div>
      <div class="card"><p class="card-label">Last activity</p><p class="card-value small" id="summary-last">-</p></div>
    </div>
  </section>

  <section id="pinned-section" hidden>
    <h2>Trace from the link</h2>
    <div id="pinned"></div>
  </section>

  <section id="questions-section">
    <h2>Questions</h2>
    <div class="filter-bar">
      <span id="book-filter" class="muted">Showing all books</span>
      <span class="muted small">Open <code>/debug#&lt;recording_id&gt;</code> to jump to one trace.</span>
    </div>
    <div id="questions"></div>
    <p id="questions-empty" class="muted" hidden>No questions yet.</p>
  </section>

  <section id="library-section">
    <div class="section-head">
      <h2>Library</h2>
      <button id="show-all-books" type="button" hidden>Show all books</button>
    </div>
    <div id="library" class="books"></div>
    <p id="library-empty" class="muted" hidden>No books synced yet.</p>
    <p id="sync-summary" class="muted small"></p>
  </section>

  <section id="tools-section">
    <h2>Tools</h2>
    <details class="disclosure" id="tools-dev">
      <summary>Dev ask</summary>
      <div class="tool">
        <form id="dev-form">
          <div class="field">
            <label for="dev-question">Question</label>
            <input id="dev-question" type="text" maxlength="2000" required placeholder="What is a thunderhead?">
          </div>
          <div class="field">
            <label for="dev-context">Context (JSON, optional)</label>
            <textarea id="dev-context" rows="4" spellcheck="false" placeholder='{"v": 1, "book": {"title": "", "authors": []}, "pos": {"page": 0}}'></textarea>
          </div>
          <div class="field">
            <label for="dev-prompt">System prompt override (optional)</label>
            <input id="dev-prompt" type="text">
          </div>
          <div class="field">
            <label for="dev-model">Model override (optional)</label>
            <input id="dev-model" type="text">
          </div>
          <div class="row">
            <button id="dev-submit" type="submit">Ask</button>
            <span id="dev-status" role="status" aria-live="polite"></span>
          </div>
        </form>
      </div>
    </details>
    <details class="disclosure" id="tools-sync">
      <summary>Library sync</summary>
      <div class="tool">
        <div class="row">
          <button id="sync" type="button">Sync now</button>
          <label><input id="dry-run" type="checkbox" checked>Dry run</label>
          <span id="sync-status" role="status" aria-live="polite"></span>
        </div>
        <div id="sync-report" class="scroll"></div>
      </div>
    </details>
    <details class="disclosure" id="tools-logs">
      <summary>Log tail</summary>
      <div class="tool">
        <label for="log-filter">Filter by id or stage</label>
        <input id="log-filter" type="text" placeholder="rec_2026">
        <pre id="logs"></pre>
      </div>
    </details>
    <details class="disclosure" id="tools-config">
      <summary>Config</summary>
      <div class="tool">
<!--config-->
      </div>
    </details>
  </section>
</main>
<noscript>
  <p class="warn">This page needs JavaScript to load the questions and the library.</p>
</noscript>
<script>
(function () {
  "use strict";

  var TOKEN_KEY = "kvb-debug-token";
  var STUCK_MS = 120000;
  var FEED_INTERVAL_MS = 2000;
  var MAX_DETAIL_FETCHES = 6;
  var PREVIEW_CHARS = 120;
  var UNKNOWN_BOOK = "Unknown book";

  var statusEl = document.getElementById("status");
  var updatedEl = document.getElementById("updated");
  var tokenEl = document.getElementById("token");
  var saveTokenBtn = document.getElementById("save-token");
  var summaryBooksEl = document.getElementById("summary-books");
  var summaryQuestionsEl = document.getElementById("summary-questions");
  var summaryAnswersEl = document.getElementById("summary-answers");
  var summaryErrorsEl = document.getElementById("summary-errors");
  var summaryLastEl = document.getElementById("summary-last");
  var filterEl = document.getElementById("book-filter");
  var showAllBtn = document.getElementById("show-all-books");
  var questionsEl = document.getElementById("questions");
  var questionsEmptyEl = document.getElementById("questions-empty");
  var libraryEl = document.getElementById("library");
  var libraryEmptyEl = document.getElementById("library-empty");
  var syncSummaryEl = document.getElementById("sync-summary");
  var toolsDevEl = document.getElementById("tools-dev");
  var toolsSyncEl = document.getElementById("tools-sync");
  var tokenBoxEl = document.getElementById("token-box");
  var pinnedSection = document.getElementById("pinned-section");
  var pinnedEl = document.getElementById("pinned");
  var reportEl = document.getElementById("sync-report");
  var logsEl = document.getElementById("logs");
  var logFilterEl = document.getElementById("log-filter");
  var devForm = document.getElementById("dev-form");
  var questionEl = document.getElementById("dev-question");
  var contextEl = document.getElementById("dev-context");
  var promptEl = document.getElementById("dev-prompt");
  var modelEl = document.getElementById("dev-model");
  var devStatusEl = document.getElementById("dev-status");
  var syncBtn = document.getElementById("sync");
  var dryRunEl = document.getElementById("dry-run");
  var syncStatusEl = document.getElementById("sync-status");

  // Summaries from /api/traces, one detail per id loaded on demand.
  var state = {
    traces: [],
    books: [],
    details: new Map(),
    pending: new Map(),
    failed: new Set(),
    selected: "",
    lastOk: 0
  };
  var rows = new Map();
  var bookCards = new Map();
  var openedHash = "";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function formatTime(value) {
    if (typeof value !== "string" || !value) return "-";
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) return value;
    return parsed.toLocaleTimeString();
  }

  function relativeTime(value) {
    var parsed = Date.parse(value || "");
    if (!isFinite(parsed)) return "-";
    var seconds = Math.round((Date.now() - parsed) / 1000);
    if (seconds < 5) return "just now";
    if (seconds < 60) return seconds + " s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + " min ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + " h ago";
    return formatTime(value);
  }

  function formatMs(value) {
    if (typeof value !== "number" || !isFinite(value)) return "-";
    return Math.round(value) + " ms";
  }

  function formatCost(value) {
    if (typeof value !== "number" || !isFinite(value)) return "-";
    if (value > 0 && value < 0.01) return "$" + value.toFixed(5);
    return "$" + value.toFixed(4);
  }

  function shortId(value) {
    if (typeof value !== "string" || !value) return "-";
    var parts = value.split("_");
    if (parts.length === 3 && parts[2].length > 8) return parts[2].slice(0, 8);
    return value.length > 8 ? value.slice(-8) : value;
  }

  function isStuck(item) {
    if (!item || item.outcome !== "pending") return false;
    var created = Date.parse(item.created_at || "");
    return isFinite(created) && Date.now() - created > STUCK_MS;
  }

  function describeError(data, fallback) {
    if (data && data.error && data.error.message) {
      return data.error.message;
    }
    return fallback;
  }

  // Book identity ------------------------------------------------------------

  function titleOf(item) {
    var title = item && typeof item.book_title === "string" ? item.book_title.trim() : "";
    return title || UNKNOWN_BOOK;
  }

  function bookKey(value) {
    return String(value === undefined || value === null ? "" : value)
      .trim()
      .toLowerCase() || UNKNOWN_BOOK.toLowerCase();
  }

  function contextOf(trace) {
    var context = trace && typeof trace === "object" ? trace.context : null;
    var parsed = context && typeof context === "object" ? context.parsed : null;
    return parsed && typeof parsed === "object" ? parsed : {};
  }

  // The summary carries the page anchor. A highlight anchor is only visible in
  // the full trace, so the badge fills in once that trace is loaded.
  function kindOf(trace, item) {
    if (trace) {
      var parsed = contextOf(trace);
      if (typeof parsed.highlight === "string" && parsed.highlight.trim()) {
        return "highlight";
      }
      var pos = parsed.pos;
      if (pos && typeof pos === "object" && typeof pos.page === "number") {
        return "page";
      }
      return "";
    }
    return item && typeof item.book_page === "number" ? "page" : "";
  }

  function bookTitleOfTrace(trace) {
    var book = contextOf(trace).book;
    var title = book && typeof book === "object" ? book.title : "";
    return typeof title === "string" && title.trim() ? title.trim() : UNKNOWN_BOOK;
  }

  function questionText(trace) {
    var llm = trace && typeof trace === "object" ? trace.llm : null;
    var prompt = llm && typeof llm === "object" ? llm.prompt : null;
    var user = prompt && typeof prompt === "object" ? prompt.user : "";
    if (typeof user !== "string") return "";
    var marker = user.lastIndexOf("Question: ");
    if (marker >= 0) return user.slice(marker + 10).trim();
    return user.trim();
  }

  function answerText(trace) {
    var llm = trace && typeof trace === "object" ? trace.llm : null;
    var text = llm && typeof llm === "object" ? llm.response_text : "";
    return typeof text === "string" ? text : "";
  }

  function previewOf(trace) {
    if (!trace) return "";
    var text = (answerText(trace) || questionText(trace)).replace(/\\s+/g, " ").trim();
    if (!text) return "-";
    if (text.length <= PREVIEW_CHARS) return text;
    return text.slice(0, PREVIEW_CHARS - 1) + "\\u2026";
  }

  // Token --------------------------------------------------------------------

  function readToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function writeToken(value) {
    try {
      window.sessionStorage.setItem(TOKEN_KEY, value);
    } catch (error) {
      statusEl.textContent = "This browser will not keep the token.";
    }
  }

  function authHeaders() {
    var token = readToken();
    return token ? { "Authorization": "Bearer " + token } : {};
  }

  // Summary ------------------------------------------------------------------

  function renderSummary() {
    var answers = 0;
    var errors = 0;
    var last = "";
    state.traces.forEach(function (item) {
      if (!item) return;
      if (item.outcome === "ok") answers += 1;
      if (item.outcome === "failed" || item.outcome === "rejected") errors += 1;
      var created = typeof item.created_at === "string" ? item.created_at : "";
      if (created && created > last) last = created;
    });
    summaryBooksEl.textContent = String(state.books.length);
    summaryQuestionsEl.textContent = String(state.traces.length);
    summaryAnswersEl.textContent = String(answers);
    summaryErrorsEl.textContent = String(errors);
    summaryLastEl.textContent = last ? relativeTime(last) : "-";
    summaryLastEl.title = last;
  }

  // Questions ----------------------------------------------------------------

  function matchesFilter(item) {
    if (!state.selected) return true;
    return bookKey(titleOf(item)) === bookKey(state.selected);
  }

  function setFilter(title) {
    state.selected = typeof title === "string" ? title : "";
    renderFilterBar();
    renderQuestions();
    updateBookCounts();
    if (state.selected) {
      questionsEl.scrollIntoView({ block: "nearest" });
    }
  }

  function renderFilterBar() {
    var active = state.selected !== "";
    filterEl.textContent = active
      ? "Showing questions about " + state.selected
      : "Showing all books";
    showAllBtn.hidden = !active;
  }

  function createRow(item) {
    var id = item.id;
    var article = el("article", "question");
    var head = el("div", "q-head");
    head.tabIndex = 0;
    head.setAttribute("role", "button");
    head.setAttribute("aria-expanded", "false");
    var cells = {
      time: el("span", "q-time"),
      book: el("button", "q-book"),
      page: el("span", "q-page"),
      kind: el("span", "badge kind"),
      preview: el("span", "q-preview"),
      outcome: el("span", "badge outcome")
    };
    cells.book.type = "button";
    cells.book.title = "Filter the questions to this book";
    cells.book.addEventListener("click", function (event) {
      event.stopPropagation();
      var current = state.selected;
      setFilter(bookKey(current) === bookKey(titleOf(item)) && current ? "" : titleOf(item));
    });
    head.append(
      cells.time,
      cells.book,
      cells.page,
      cells.kind,
      cells.preview,
      cells.outcome
    );
    head.addEventListener("click", function () {
      toggleRow(id);
    });
    head.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        toggleRow(id);
      }
    });
    var panel = el("div", "panel");
    panel.hidden = true;
    article.append(head, panel);
    return {
      id: id,
      item: item,
      article: article,
      head: head,
      panel: panel,
      cells: cells,
      signature: ""
    };
  }

  function updateRow(entry, item) {
    entry.item = item;
    entry.cells.time.textContent = formatTime(item.created_at);
    entry.cells.time.title = item.created_at || "";
    entry.cells.book.textContent = titleOf(item);
    entry.cells.page.textContent =
      typeof item.book_page === "number" ? "p." + item.book_page : "-";
    var trace = state.details.get(item.id);
    var kind = kindOf(trace, item);
    entry.cells.kind.textContent = kind || "\\u2014";
    entry.cells.kind.className = "badge kind" + (kind ? " kind-" + kind : "");
    entry.cells.preview.textContent = previewOf(trace);
    var outcome = typeof item.outcome === "string" && item.outcome ? item.outcome : "pending";
    entry.cells.outcome.textContent = outcome;
    entry.cells.outcome.className = "badge outcome outcome-" + outcome;
    entry.article.classList.toggle("stuck", isStuck(item));

    var signature = JSON.stringify(item);
    var changed = signature !== entry.signature;
    entry.signature = signature;
    if (changed && !entry.panel.hidden && item.outcome === "pending") {
      loadPanel(item.id, true);
    }
  }

  function renderQuestions() {
    var visible = state.traces.filter(matchesFilter);
    var shown = new Set();

    visible.forEach(function (item, index) {
      if (!item || typeof item.id !== "string") return;
      shown.add(item.id);
      var entry = rows.get(item.id);
      if (!entry) {
        entry = createRow(item);
        rows.set(item.id, entry);
      }
      updateRow(entry, item);
      if (questionsEl.children[index] !== entry.article) {
        questionsEl.insertBefore(entry.article, questionsEl.children[index] || null);
      }
    });

    // A row outside the filter leaves the document but keeps its entry, so an
    // open panel and its loaded trace survive a filter change. A row that left
    // the feed is dropped.
    var inFeed = new Set();
    state.traces.forEach(function (item) {
      if (item && typeof item.id === "string") inFeed.add(item.id);
    });
    rows.forEach(function (entry, id) {
      if (!inFeed.has(id)) {
        entry.article.remove();
        rows.delete(id);
        state.details.delete(id);
        return;
      }
      if (!shown.has(id)) entry.article.remove();
    });

    questionsEmptyEl.hidden = visible.length !== 0;
    questionsEmptyEl.textContent = state.selected
      ? "No questions for " + state.selected + " yet."
      : "No questions yet.";
  }

  function toggleRow(id) {
    var entry = rows.get(id);
    if (!entry) return;
    var open = entry.panel.hidden;
    entry.panel.hidden = !open;
    entry.article.classList.toggle("open", open);
    entry.head.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) loadPanel(id);
  }

  async function loadPanel(id, force) {
    var entry = rows.get(id);
    if (!entry) return;
    if (force) state.details.delete(id);
    try {
      var trace = await ensureDetail(id);
      fillPanel(entry.panel, trace);
      if (entry.item) updateRow(entry, entry.item);
    } catch (error) {
      entry.panel.replaceChildren(el("p", "warn", String(error.message || error)));
    }
  }

  function ensureDetail(id) {
    if (state.details.has(id)) return Promise.resolve(state.details.get(id));
    if (state.pending.has(id)) return state.pending.get(id);
    var request = fetch("/api/traces/" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (response) {
        return response.json().catch(function () { return null; }).then(function (data) {
          if (!response.ok) {
            throw new Error(describeError(data, "Could not load the trace."));
          }
          state.details.set(id, data);
          return data;
        });
      })
      .finally(function () {
        state.pending.delete(id);
      });
    state.pending.set(id, request);
    return request;
  }

  // Previews need the answer text, which only the full trace carries. Load a
  // few at a time, newest first, and stop once every visible row is cached.
  function prefetchOne(id) {
    ensureDetail(id).then(function () {
      var entry = rows.get(id);
      if (entry && entry.item) updateRow(entry, entry.item);
      prefetchDetails();
    }, function () {
      state.failed.add(id);
      prefetchDetails();
    });
  }

  function prefetchDetails() {
    var started = 0;
    state.traces.forEach(function (item) {
      if (started >= MAX_DETAIL_FETCHES) return;
      var id = item && typeof item.id === "string" ? item.id : "";
      if (!id || state.failed.has(id)) return;
      if (state.details.has(id) || state.pending.has(id)) return;
      started += 1;
      prefetchOne(id);
    });
  }

  // Library ------------------------------------------------------------------

  function addBookField(container, name, value) {
    container.append(el("dt", null, name));
    var valueEl = el("dd", null, value);
    container.append(valueEl);
    return valueEl;
  }

  function createBookCard(book, title) {
    var card = el("article", "book");
    var head = el("button", "book-head");
    head.type = "button";
    head.append(el("span", "book-title", title));
    var authors = Array.isArray(book.authors)
      ? book.authors.filter(function (name) {
          return typeof name === "string" && name.trim();
        })
      : [];
    head.append(el("span", "book-authors", authors.length ? authors.join(", ") : "Unknown author"));
    head.addEventListener("click", function () {
      var same = state.selected && bookKey(state.selected) === bookKey(title);
      setFilter(same ? "" : title);
    });

    var meta = el("dl", "book-meta");
    addBookField(meta, "Characters", typeof book.chars === "number" ? book.chars : 0);
    var count = addBookField(meta, "Questions", 0);
    var asked = addBookField(meta, "Last asked", "-");
    addBookField(meta, "Last synced", formatTime(book.updated_at));

    var detail = el("div", "book-detail");
    detail.hidden = true;
    var detailMeta = el("dl", "meta");
    addField(detailMeta, "Key", book.key);
    addField(detailMeta, "Format", book.format);
    addField(detailMeta, "Source", book.source_path);
    addField(detailMeta, "Added", formatTime(book.added_at));
    addField(detailMeta, "Updated", formatTime(book.updated_at));
    addField(detailMeta, "Aliases", Array.isArray(book.aliases) ? book.aliases.length : 0);
    detail.append(detailMeta);

    card.append(head, meta, detail);
    return { card: card, count: count, asked: asked, detail: detail };
  }

  function renderBooks() {
    libraryEl.replaceChildren();
    bookCards.clear();
    libraryEmptyEl.hidden = state.books.length !== 0;
    state.books.forEach(function (raw) {
      var book = raw && typeof raw === "object" ? raw : {};
      var title = typeof book.title === "string" && book.title.trim()
        ? book.title.trim()
        : UNKNOWN_BOOK;
      var entry = createBookCard(book, title);
      bookCards.set(bookKey(title), entry);
      libraryEl.append(entry.card);
    });
    updateBookCounts();
  }

  function updateBookCounts() {
    var counts = new Map();
    var asked = new Map();
    state.traces.forEach(function (item) {
      if (!item) return;
      var key = bookKey(titleOf(item));
      counts.set(key, (counts.get(key) || 0) + 1);
      var created = typeof item.created_at === "string" ? item.created_at : "";
      if (created && (!asked.has(key) || created > asked.get(key))) {
        asked.set(key, created);
      }
    });
    bookCards.forEach(function (entry, key) {
      entry.count.textContent = String(counts.get(key) || 0);
      entry.asked.textContent = asked.has(key) ? relativeTime(asked.get(key)) : "-";
      entry.asked.title = asked.get(key) || "";
      var selected = state.selected !== "" && bookKey(state.selected) === key;
      entry.card.classList.toggle("selected", selected);
      entry.detail.hidden = !selected;
    });
  }

  function renderSyncSummary(report) {
    if (!report || typeof report !== "object") {
      syncSummaryEl.textContent = "No sync report yet.";
      return;
    }
    var counts = report.counts && typeof report.counts === "object" ? report.counts : {};
    var parts = Object.keys(counts).map(function (name) {
      return name + " " + counts[name];
    });
    syncSummaryEl.textContent =
      "Last sync " +
      formatTime(report.finished_at) +
      (report.dry_run ? " (dry run)" : "") +
      (parts.length ? ": " + parts.join(", ") : "");
  }

  // Trace panel -------------------------------------------------------------

  function addField(container, name, value) {
    container.append(el("dt", null, name));
    var missing = value === undefined || value === null || value === "";
    container.append(el("dd", null, missing ? "-" : String(value)));
  }

  function jsonText(value) {
    try {
      return JSON.stringify(value === undefined ? {} : value, null, 2);
    } catch (error) {
      return String(value);
    }
  }

  function stageTable(stages) {
    var table = el("table");
    var head = el("thead");
    var headRow = el("tr");
    ["Stage", "Duration", "Outcome", "Detail"].forEach(function (label) {
      headRow.append(el("th", null, label));
    });
    head.append(headRow);
    table.append(head);

    var body = el("tbody");
    (Array.isArray(stages) ? stages : []).forEach(function (raw) {
      var stage = raw && typeof raw === "object" ? raw : {};
      var row = el("tr");
      row.append(el("td", null, stage.name || "-"));
      row.append(el("td", null, formatMs(stage.dur_ms)));
      row.append(el("td", null, stage.outcome || "-"));
      row.append(el("td", "mono", jsonText(stage.detail)));
      body.append(row);
    });
    table.append(body);
    return table;
  }

  // Tool calls stay compact on purpose: the query, the scope, the hit count
  // and the duration. The passages themselves stay in the answer, not here.
  function bookSearchTable(tools) {
    var table = el("table");
    var head = el("thead");
    var headRow = el("tr");
    ["Round", "Query", "Scope", "Hits", "Duration"].forEach(function (label) {
      headRow.append(el("th", null, label));
    });
    head.append(headRow);
    table.append(head);

    var body = el("tbody");
    (Array.isArray(tools) ? tools : []).forEach(function (raw) {
      var tool = raw && typeof raw === "object" ? raw : {};
      var arguments_ = tool.arguments && typeof tool.arguments === "object"
        ? tool.arguments
        : {};
      var row = el("tr");
      row.append(el("td", null, typeof tool.round === "number" ? tool.round : "-"));
      row.append(el("td", null, typeof arguments_.query === "string" ? arguments_.query : "-"));
      row.append(el("td", null, typeof arguments_.scope === "string" ? arguments_.scope : "all"));
      row.append(el("td", null, typeof tool.hits === "number" ? tool.hits : 0));
      row.append(el("td", null, formatMs(tool.ms)));
      body.append(row);
    });
    table.append(body);
    return table;
  }

  function fillPanel(panel, trace) {
    panel.replaceChildren();

    var meta = el("dl", "meta");
    addField(meta, "Trace", trace.id);
    addField(meta, "Origin", trace.origin);
    addField(meta, "Outcome", trace.outcome);
    addField(meta, "Book", bookTitleOfTrace(trace));
    addField(meta, "Created", formatTime(trace.created_at));
    panel.append(meta);

    panel.append(el("h3", null, "Context"));
    panel.append(el("pre", "block", jsonText(trace.context)));

    panel.append(el("h3", null, "Book match"));
    var book = trace.book && typeof trace.book === "object" ? trace.book : {};
    var bookMeta = el("dl", "meta");
    addField(bookMeta, "Key", book.key);
    addField(bookMeta, "Match", book.match);
    panel.append(bookMeta);

    panel.append(el("h3", null, "Stages"));
    panel.append(stageTable(trace.stages));

    panel.append(el("h3", null, "Resolved passage"));
    var resolve = trace.resolve && typeof trace.resolve === "object" ? trace.resolve : {};
    var resolveMeta = el("dl", "meta");
    addField(resolveMeta, "Rung", resolve.rung);
    addField(resolveMeta, "Reason", resolve.reason);
    var passage = typeof resolve.passage === "string" ? resolve.passage : "";
    addField(resolveMeta, "Characters", passage.length);
    panel.append(resolveMeta);
    if (passage) panel.append(el("pre", "block", passage));

    var llm = trace.llm && typeof trace.llm === "object" ? trace.llm : {};
    var prompt = llm.prompt && typeof llm.prompt === "object" ? llm.prompt : {};
    var searchTools = Array.isArray(llm.tools) ? llm.tools : [];
    if (searchTools.length) {
      panel.append(el("h3", null, "Book searches"));
      var searchMeta = el("dl", "meta");
      addField(searchMeta, "Calls", searchTools.length);
      addField(searchMeta, "Model rounds", llm.rounds);
      if (llm.tools_unsupported) {
        addField(searchMeta, "Tools", "rejected by the model, plain answer used");
      }
      panel.append(searchMeta);
      panel.append(bookSearchTable(searchTools));
    }

    panel.append(el("h3", null, "Model messages"));
    var llmMeta = el("dl", "meta");
    addField(llmMeta, "Model", llm.model);
    addField(llmMeta, "Cost", formatCost(llm.cost_usd));
    addField(llmMeta, "Cost source", llm.cost_source);
    addField(llmMeta, "Usage", jsonText(llm.usage));
    var answer = trace.answer && typeof trace.answer === "object" ? trace.answer : {};
    addField(
      llmMeta,
      "Answer characters",
      String(answer.chars_sent || 0) + " of " + String(answer.chars_declared || 0)
    );
    panel.append(llmMeta);
    panel.append(el("h4", null, "System"));
    panel.append(el("pre", "block", typeof prompt.system === "string" ? prompt.system : ""));
    panel.append(el("h4", null, "User"));
    panel.append(el("pre", "block", typeof prompt.user === "string" ? prompt.user : ""));
    panel.append(el("h4", null, "Response"));
    panel.append(
      el("pre", "block", typeof llm.response_text === "string" ? llm.response_text : "")
    );

    var rerun = el("button", null, "Re-run in the dev form");
    rerun.type = "button";
    rerun.addEventListener("click", function () {
      prefill(trace);
    });
    panel.append(rerun);
  }

  function prefill(trace) {
    var parsed = contextOf(trace);
    contextEl.value = jsonText(parsed);
    questionEl.value = questionText(trace);
    var llm = trace.llm && typeof trace.llm === "object" ? trace.llm : {};
    var prompt = llm.prompt && typeof llm.prompt === "object" ? llm.prompt : {};
    promptEl.value = typeof prompt.system === "string" ? prompt.system : "";
    modelEl.value = typeof llm.model === "string" ? llm.model : "";
    toolsDevEl.open = true;
    devStatusEl.textContent = "Prefilled from " + (trace.id || "the trace") + ".";
    questionEl.focus();
    questionEl.scrollIntoView({ block: "center" });
  }

  // Feed ---------------------------------------------------------------------

  async function loadFeed() {
    try {
      var response = await fetch("/api/traces?limit=50", { cache: "no-store" });
      var data = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(describeError(data, "Could not load traces."));
      }
      state.traces = Array.isArray(data.traces) ? data.traces : [];
      state.lastOk = Date.now();
      statusEl.textContent =
        "Receiver reachable - " +
        state.traces.length +
        (state.traces.length === 1 ? " question" : " questions") +
        " in the feed.";
      renderSummary();
      renderQuestions();
      updateBookCounts();
      prefetchDetails();
      openFromHash();
      tickAgo();
    } catch (error) {
      statusEl.textContent = "Receiver unreachable - " + String(error.message || error);
      tickAgo();
    }
  }

  async function poll() {
    await loadFeed();
    window.setTimeout(poll, FEED_INTERVAL_MS);
  }

  function tickAgo() {
    if (!state.lastOk) {
      updatedEl.textContent = "";
      updatedEl.classList.remove("stale");
      return;
    }
    var seconds = Math.max(0, Math.round((Date.now() - state.lastOk) / 1000));
    updatedEl.textContent = seconds < 1 ? "updated now" : "updated " + seconds + " s ago";
    updatedEl.classList.toggle("stale", seconds > 10);
  }

  // Deep links ---------------------------------------------------------------

  function hashId() {
    var value = window.location.hash.replace(/^#/, "");
    if (!value) return "";
    try {
      return decodeURIComponent(value);
    } catch (error) {
      return value;
    }
  }

  function openFromHash() {
    var id = hashId();
    if (!id || id === openedHash) return;
    openedHash = id;
    openTrace(id);
  }

  async function openTrace(id) {
    var entry = rows.get(id);
    if (entry && !entry.article.isConnected) {
      setFilter("");
      entry = rows.get(id);
    }
    if (entry) {
      pinnedSection.hidden = true;
      pinnedEl.replaceChildren();
      if (entry.panel.hidden) toggleRow(id);
      entry.article.scrollIntoView({ block: "nearest" });
      return;
    }
    try {
      var trace = await ensureDetail(id);
      pinnedSection.hidden = false;
      pinnedEl.replaceChildren();
      var panel = el("div", "panel");
      panel.append(el("h3", null, id));
      fillPanel(panel, trace);
      pinnedEl.append(panel);
      pinnedSection.scrollIntoView({ block: "nearest" });
    } catch (error) {
      pinnedSection.hidden = false;
      pinnedEl.replaceChildren(el("p", "warn", String(error.message || error)));
    }
  }

  // Dev ask -----------------------------------------------------------------

  async function submitDevAsk(event) {
    event.preventDefault();
    var token = readToken();
    if (!token) {
      devStatusEl.textContent = "Enter the bearer token first.";
      toolsDevEl.open = true;
      tokenBoxEl.open = true;
      tokenEl.focus();
      return;
    }

    var payload = { question: questionEl.value };
    var rawContext = contextEl.value.trim();
    if (rawContext) {
      try {
        payload.context = JSON.parse(rawContext);
      } catch (error) {
        devStatusEl.textContent = "Context is not valid JSON.";
        return;
      }
      if (!payload.context || typeof payload.context !== "object" ||
          Array.isArray(payload.context)) {
        devStatusEl.textContent = "Context must be a JSON object.";
        return;
      }
    }
    if (promptEl.value.trim()) payload.prompt = promptEl.value.trim();
    if (modelEl.value.trim()) payload.model = modelEl.value.trim();

    devStatusEl.textContent = "Asking...";
    try {
      var response = await fetch("/v1/dev/ask", {
        method: "POST",
        cache: "no-store",
        headers: Object.assign(
          { "Content-Type": "application/json" },
          authHeaders()
        ),
        body: JSON.stringify(payload)
      });
      var data = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(describeError(data, "The dev ask failed (" + response.status + ")."));
      }
      devStatusEl.textContent = "Started " + shortId(data.id) + ".";
      await loadFeed();
    } catch (error) {
      devStatusEl.textContent = String(error.message || error);
    }
  }

  // Sync --------------------------------------------------------------------

  function renderReport(report) {
    reportEl.replaceChildren();
    if (!report || typeof report !== "object") {
      reportEl.append(el("p", "muted", "No sync report yet."));
      return;
    }

    var counts = report.counts && typeof report.counts === "object" ? report.counts : {};
    var meta = el("dl", "meta");
    addField(meta, "Source", report.source_dir);
    addField(meta, "Dry run", report.dry_run ? "yes" : "no");
    addField(meta, "Reason", report.reason);
    addField(meta, "Finished", formatTime(report.finished_at));
    addField(
      meta,
      "Counts",
      Object.keys(counts).map(function (name) {
        return name + "=" + counts[name];
      }).join(" ")
    );
    reportEl.append(meta);

    var rowsData = Array.isArray(report.rows) ? report.rows : [];
    if (!rowsData.length) return;
    var table = el("table");
    var head = el("thead");
    var headRow = el("tr");
    ["Path", "Status", "Reason", "Book", "Duration"].forEach(function (label) {
      headRow.append(el("th", null, label));
    });
    head.append(headRow);
    table.append(head);

    var body = el("tbody");
    rowsData.forEach(function (raw) {
      var rowData = raw && typeof raw === "object" ? raw : {};
      var row = el("tr");
      row.append(el("td", null, rowData.path || "-"));
      row.append(el("td", null, rowData.status || "-"));
      row.append(el("td", null, rowData.reason || "-"));
      row.append(el("td", "mono", rowData.book_key || "-"));
      row.append(el("td", null, formatMs(rowData.ms)));
      body.append(row);
    });
    table.append(body);
    reportEl.append(table);
  }

  async function loadLibrary() {
    try {
      var response = await fetch("/api/library", { cache: "no-store" });
      var data = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(describeError(data, "Could not load the library."));
      }
      state.books = Array.isArray(data.books) ? data.books : [];
      renderBooks();
      renderReport(data.last_sync);
      renderSyncSummary(data.last_sync);
      renderSummary();
    } catch (error) {
      libraryEl.replaceChildren(el("p", "warn", String(error.message || error)));
    }
  }

  async function runSync() {
    var token = readToken();
    if (!token) {
      syncStatusEl.textContent = "Enter the bearer token first.";
      toolsSyncEl.open = true;
      tokenBoxEl.open = true;
      tokenEl.focus();
      return;
    }
    syncBtn.disabled = true;
    syncStatusEl.textContent = dryRunEl.checked ? "Dry run..." : "Syncing...";
    try {
      var response = await fetch(
        "/api/library/sync?dry_run=" + (dryRunEl.checked ? "1" : "0"),
        { method: "POST", cache: "no-store", headers: authHeaders() }
      );
      var report = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(describeError(report, "The sync failed (" + response.status + ")."));
      }
      renderReport(report);
      renderSyncSummary(report);
      syncStatusEl.textContent = report.dry_run ? "Dry run done." : "Sync done.";
      await loadLibrary();
      await loadLogs();
    } catch (error) {
      syncStatusEl.textContent = String(error.message || error);
    } finally {
      syncBtn.disabled = false;
    }
  }

  // Logs --------------------------------------------------------------------

  async function loadLogs() {
    var query = logFilterEl.value.trim();
    var url = "/api/logs?limit=200";
    if (query) url += "&q=" + encodeURIComponent(query);
    try {
      var response = await fetch(url, { cache: "no-store" });
      var data = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(describeError(data, "Could not load the logs."));
      }
      var lines = Array.isArray(data.lines) ? data.lines : [];
      logsEl.textContent = lines.length ? lines.join("\\n") : "(no log lines)";
    } catch (error) {
      logsEl.textContent = String(error.message || error);
    }
  }

  // Wiring ------------------------------------------------------------------

  tokenEl.value = readToken();
  tokenEl.addEventListener("change", function () {
    writeToken(tokenEl.value.trim());
    statusEl.textContent = "Token saved in this tab.";
  });
  saveTokenBtn.addEventListener("click", function () {
    writeToken(tokenEl.value.trim());
    statusEl.textContent = tokenEl.value.trim()
      ? "Token saved in this tab."
      : "Token cleared.";
  });

  devForm.addEventListener("submit", submitDevAsk);
  syncBtn.addEventListener("click", runSync);
  logFilterEl.addEventListener("input", function () {
    loadLogs();
  });
  showAllBtn.addEventListener("click", function () {
    setFilter("");
  });
  window.addEventListener("hashchange", function () {
    openedHash = "";
    openFromHash();
  });
  window.setInterval(tickAgo, 1000);

  renderFilterBar();
  poll();
  loadLibrary();
  loadLogs();
})();
</script>"""
