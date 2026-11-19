"""FastAPI application for the Kindle voice-bridge receiver.

The app implements the v1 contract in ``docs/protocol.md``. The contract has one
authenticated upload endpoint and one unauthenticated health probe. The app
bounds every body read, validates the WAV structure and writes local files
atomically. It never logs the token, the ``Authorization`` header or audio
bytes, and it never retries.

A local development review UI (``GET /``) and its listing, playback and delete
routes are attached from :mod:`voice_bridge_server.review`. They are outside the
upload contract and are unauthenticated; see that module for the safety rules.

The debug page and its JSON routes, the dev ask and the library sync endpoint are
attached from :mod:`voice_bridge_server.debug_routes`. They share the trace store
and the book library that this module creates once per app, and a dev ask runs
the same transcription and answer callables as an upload.

One ask is also an observability unit. After the audio is stored, the receiver
starts a trace (see :mod:`voice_bridge_server.trace`) and records the ``receive``,
``validate``, ``store``, ``context`` and ``resolve`` stages. The optional
``X-KVB-Context`` header carries the reading context; a malformed header is
recorded and written to a sidecar, and it never rejects the upload.

Binding and transport (uvicorn ``--host``/``--port``, TLS termination) are
outside this module. ``create_app`` never reads the environment. Use
:func:`create_app_from_env` for the uvicorn factory.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import pathlib
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from .answer import DEFAULT_ANSWER_MODEL, DEFAULT_REASONING_EFFORT
from .answers import answer_recording, read_answer
from .audio import WavError, parse_wav
from .config import Settings, token_matches
from .context import build_context_block, decode_header, resolve_passage
from .debug_routes import register_debug_routes
from .library import Library
from .review import InvalidRecordingId, recording_path, register_review_routes
from .search import book_search
from .storage import LocalFileStore, RecordingStore, new_recording_id
from .trace import TraceStore, configure_logging
from .transcribe import DEFAULT_MODEL as DEFAULT_TRANSCRIBE_MODEL
from .transcripts import trace_safely, transcribe_recording

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_API_VERSION = "v1"
_WAV_CODES = frozenset(
    {
        "empty_body",
        "incomplete_body",
        "invalid_wav",
        "unsupported_wav_format",
        "duration_exceeds_limit",
    }
)
_HTTP_ERROR_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    500: "internal_error",
}
_HTTP_ERROR_MESSAGES = {
    400: "The request is invalid.",
    401: "Missing or invalid bearer token.",
    404: "Resource not found.",
    405: "Method not allowed.",
    413: "Request body exceeds the maximum size.",
    415: "Unsupported media type.",
    500: "An internal error occurred.",
}
_WRITE_PROBE_PREFIX = ".kvb-write-probe-"

#: Header that carries the optional reading context payload.
CONTEXT_HEADER = "x-kvb-context"

#: Schema version written to the ``.context.json`` sidecar.
CONTEXT_SCHEMA = "v1"

#: Origin recorded for an ask that arrived through the upload endpoint.
ORIGIN_KINDLE = "kindle"


def create_app(
    settings: Settings,
    *,
    store: RecordingStore | None = None,
    transcribe: Callable[..., object] | None = None,
    answer: Callable[..., object] | None = None,
    resolve: Callable[[dict], object] | None = None,
    trace_store: TraceStore | None = None,
) -> FastAPI:
    """Build the ASGI application and validate the storage directory.

    ``transcribe`` is an optional callable that receives the stored path of each
    accepted recording. It runs as a background task after the response is sent,
    so the upload contract never depends on it. ``None`` disables transcription.
    A callable that declares ``trace`` and ``trace_store`` receives the trace of
    the current ask.

    ``answer`` is an optional callable that receives ``(stored_path,
    transcript_text)`` and, when the ask carried a reading context, the
    ``context_block`` keyword. It runs in the same background task, after a
    successful transcription with a non-empty transcript, and is never called
    otherwise.

    ``resolve`` maps a parsed context payload to ``(passage, rung, reason,
    book_key, match)``, optionally followed by a book search callable. ``None``
    means no library is configured. ``trace_store`` defaults to a store in the
    storage directory that keeps the model response and honours
    ``settings.debug_text``.

    A resolver that names a book also yields the search callable that the answer
    callable receives as ``book_search``; without a called ``resolve`` the ask
    is one plain model call.

    The book library defaults to ``settings.library_dir``, or to a ``library``
    directory under the storage directory when it is unset. The trace store and
    the library are created once, and the debug routes share them.
    """

    _prepare_storage(settings.storage_dir)
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Kindle Voice Bridge",
        version=_API_VERSION,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        # The router inherits this, so a POST body is never answered with a
        # 307 redirect.
        redirect_slashes=False,
    )

    traces = (
        trace_store
        if trace_store is not None
        else TraceStore(
            settings.storage_dir, raw=True, text=settings.debug_text
        )
    )
    library_root = (
        settings.library_dir
        if settings.library_dir is not None
        else settings.storage_dir / "library"
    )

    app.state.settings = settings
    app.state.transcribe = transcribe
    app.state.answer = answer
    app.state.resolve = resolve
    app.state.store = (
        store if store is not None else LocalFileStore(settings.storage_dir)
    )
    app.state.trace_store = traces
    app.state.library = Library(library_root)

    def run_pipeline(
        path: pathlib.Path,
        *,
        origin: str = ORIGIN_KINDLE,
        context: Mapping[str, Any] | None = None,
        question: str | None = None,
        prompt: str | None = None,
        model: str | None = None,
        recording_id: str | None = None,
        context_block: str | None = None,
        book_search: Callable[..., list] | None = None,
    ) -> None:
        """Run one stored ask through resolution, transcription and answering.

        The upload path passes the trace id and the context block it has already
        recorded. A dev ask passes its parsed context and no trace id, so the
        resolution stages are recorded here, and it passes ``question`` because a
        silent clip carries no speech to transcribe. ``origin`` decides whether a
        pipeline that cannot run records a skipped stage: a dev ask does, and an
        upload keeps the stage list it always had.
        """

        # A dev ask can be deleted before its background task runs. Do not
        # create a trace for a recording that no longer exists.
        if not path.exists():
            return

        trace_id = recording_id if recording_id is not None else path.stem
        if context_block is None and context is not None:
            context_block, book_search = _record_resolve(
                traces, trace_id, dict(context), resolve
            )

        hook = transcribe if question is None else _dev_transcriber(question)
        if hook is None:
            if origin != ORIGIN_KINDLE:
                for name in ("transcribe", "answer"):
                    _record_stage(
                        traces,
                        trace_id,
                        name,
                        detail={"reason": "no-openrouter-key"},
                        outcome="skipped",
                    )
            _trace_outcome(traces, trace_id, "ok")
            return

        _run_pipeline(
            hook,
            answer,
            path,
            trace=trace_id,
            trace_store=traces,
            context_block=context_block,
            book_search=book_search,
            prompt=prompt,
            model=model,
        )

    app.state.pipeline = run_pipeline

    register_review_routes(app, settings)
    register_debug_routes(app, settings, traces, run_pipeline)

    @app.post("/v1/recordings", include_in_schema=False)
    @app.post("/v1/recordings/", include_in_schema=False)
    async def upload_recording(
        request: Request, background: BackgroundTasks
    ) -> Response:
        return await _handle_upload(
            request,
            settings,
            app.state.store,
            background,
            app.state.transcribe,
            app.state.answer,
            app.state.resolve,
            app.state.trace_store,
            app.state.pipeline,
        )

    @app.get("/v1/recordings/{recording_id}/answer", include_in_schema=False)
    async def get_answer(recording_id: str, request: Request) -> Response:
        authorization_values = request.headers.getlist("authorization")
        if len(authorization_values) != 1 or not token_matches(
            authorization_values[0], settings.token
        ):
            return _error_response(
                401,
                "unauthorized",
                "Missing or invalid bearer token.",
                extra_headers={
                    "WWW-Authenticate": 'Bearer realm="voice-bridge"'
                },
            )

        try:
            path = recording_path(settings.storage_dir, recording_id)
        except InvalidRecordingId:
            return _error_response(404, "not_found", "Resource not found.")
        if not path.is_file():
            return _error_response(404, "not_found", "Resource not found.")

        return _answer_response(recording_id, read_answer(path))

    @app.api_route(
        "/healthz",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def healthz(request: Request) -> Response:
        if request.method == "HEAD":
            return Response(
                status_code=200,
                headers={"Cache-Control": "no-store"},
            )
        return _json_response(
            {"status": "ok", "api_version": _API_VERSION},
            200,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        code = _HTTP_ERROR_CODES.get(exc.status_code, "internal_error")
        message = _HTTP_ERROR_MESSAGES.get(
            exc.status_code, "Request failed."
        )
        extra_headers = dict(exc.headers or {})
        return _error_response(
            exc.status_code,
            code,
            message,
            extra_headers=extra_headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> Response:
        logger.error("internal_error type=%s", type(exc).__name__)
        return _error_response(
            500, "internal_error", "An internal error occurred."
        )

    return app


def create_app_from_env(
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the app from ``KVB_*`` environment variables.

    ``uvicorn --factory`` uses this function. A missing or blank required
    variable raises ``RuntimeError`` that names the variable only. An invalid
    value raises ``ValueError`` from :class:`Settings`.

    ``KVB_LIBRARY_DIR`` defaults to ``library`` under ``KVB_STORAGE_DIR`` and
    stores extracted books. ``KVB_LIBRARY_SOURCE`` defaults to
    ``library-source`` under the storage directory and is the folder the sync
    scans for EPUB files. ``KVB_ANSWER_MODEL`` and
    ``KVB_ANSWER_REASONING_EFFORT`` set the answer model and its effort;
    ``none`` or ``off`` omits the effort. ``KVB_DEBUG_TEXT`` defaults to true
    and turns the prompt, passage and context text in the trace files off when
    it is false. ``KVB_LOG_LEVEL`` defaults to ``INFO``.
    """

    env = os.environ if environ is None else environ

    token = _required_env(env, "KVB_TOKEN")
    storage_dir = _required_env(env, "KVB_STORAGE_DIR")
    max_body_bytes = _optional_int(env, "KVB_MAX_BODY_BYTES")
    max_duration_seconds = _optional_float(env, "KVB_MAX_DURATION_SECONDS")

    settings_kwargs: dict[str, object] = {}
    if max_body_bytes is not None:
        settings_kwargs["max_body_bytes"] = max_body_bytes
    if max_duration_seconds is not None:
        settings_kwargs["max_duration_seconds"] = max_duration_seconds

    library_dir = env.get("KVB_LIBRARY_DIR", "").strip()
    library_root = (
        pathlib.Path(library_dir)
        if library_dir
        else pathlib.Path(storage_dir) / "library"
    )
    library_source = env.get("KVB_LIBRARY_SOURCE", "").strip()
    source_root = (
        pathlib.Path(library_source)
        if library_source
        else pathlib.Path(storage_dir) / "library-source"
    )
    try:
        source_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    settings_kwargs["library_dir"] = library_root
    settings_kwargs["library_source"] = source_root
    settings_kwargs["debug_text"] = _optional_bool(env, "KVB_DEBUG_TEXT", True)
    log_level = env.get("KVB_LOG_LEVEL", "").strip()
    if log_level:
        settings_kwargs["log_level"] = log_level

    settings = Settings(
        token=token,
        storage_dir=storage_dir,
        **settings_kwargs,
    )
    return create_app(
        settings,
        transcribe=_transcriber_from_env(env),
        answer=_answerer_from_env(env),
        resolve=_library_resolver(Library(library_root)),
    )


def _transcriber_from_env(
    env: Mapping[str, str],
) -> Callable[..., object] | None:
    """Return a transcription callable, or ``None`` when it is not configured.

    Transcription is opt-in: without ``OPENROUTER_API_KEY`` the receiver behaves
    exactly as before and never makes an outbound request. ``KVB_TRANSCRIBE_MODEL``
    overrides the model.
    """

    api_key = env.get("OPENROUTER_API_KEY", "")
    if not api_key.strip():
        return None
    model = env.get("KVB_TRANSCRIBE_MODEL", "").strip() or DEFAULT_TRANSCRIBE_MODEL

    def transcribe(
        path: pathlib.Path,
        trace: str | None = None,
        trace_store: TraceStore | None = None,
    ) -> object:
        return transcribe_recording(
            path, api_key, model=model, trace=trace, trace_store=trace_store
        )

    return transcribe


def _answerer_from_env(
    env: Mapping[str, str],
) -> Callable[..., object] | None:
    """Return an answer callable, or ``None`` when it is not configured.

    Answering shares ``OPENROUTER_API_KEY`` with transcription: without a key
    neither outbound call happens. ``KVB_ANSWER_MODEL`` overrides the model.
    ``KVB_ANSWER_REASONING_EFFORT`` overrides the effort; a blank value omits
    the parameter.
    """

    api_key = env.get("OPENROUTER_API_KEY", "")
    if not api_key.strip():
        return None
    model = env.get("KVB_ANSWER_MODEL", "").strip() or DEFAULT_ANSWER_MODEL
    effort_raw = env.get("KVB_ANSWER_REASONING_EFFORT", "")
    effort = (
        effort_raw.strip()
        if "KVB_ANSWER_REASONING_EFFORT" in env
        else DEFAULT_REASONING_EFFORT
    )
    if effort.lower() in {"none", "off"}:
        effort = ""

    def answer(
        path: pathlib.Path,
        transcript: str,
        context_block: str | None = None,
        trace: str | None = None,
        trace_store: TraceStore | None = None,
        book_search: Callable[..., list] | None = None,
    ) -> object:
        return answer_recording(
            path,
            transcript,
            api_key,
            model=model,
            context_block=context_block,
            trace=trace,
            trace_store=trace_store,
            book_search=book_search,
            reasoning_effort=effort,
        )

    return answer


async def _handle_upload(
    request: Request,
    settings: Settings,
    store: RecordingStore,
    background: BackgroundTasks | None = None,
    transcribe: Callable[..., object] | None = None,
    answer: Callable[..., object] | None = None,
    resolve: Callable[[dict], object] | None = None,
    trace_store: TraceStore | None = None,
    pipeline: Callable[..., None] | None = None,
) -> Response:
    # 2. Authentication runs before every body check.
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) != 1 or not token_matches(
        authorization_values[0], settings.token
    ):
        return _error_response(
            401,
            "unauthorized",
            "Missing or invalid bearer token.",
            extra_headers={"WWW-Authenticate": 'Bearer realm="voice-bridge"'},
        )

    # 2b. The optional reading context header, read after authentication.
    # Starlette lowercases header names. A bad header is recorded later and
    # never rejects the upload.
    raw_context = request.headers.get(CONTEXT_HEADER)
    header_bytes = (
        len(raw_context.encode("utf-8")) if isinstance(raw_context, str) else 0
    )
    receive_started = time.perf_counter()

    # 3. Content-Encoding and Content-Type.
    content_encodings = request.headers.getlist("content-encoding")
    if content_encodings and any(
        value.strip().lower() != "identity"
        for value in content_encodings
    ):
        return _error_response(
            415,
            "unsupported_media_type",
            "Content-Encoding must be absent or identity.",
        )
    if not _is_wav_content_type(request.headers.get("content-type")):
        return _error_response(
            415,
            "unsupported_media_type",
            "Content-Type must be audio/wav.",
        )

    # 4. A declared length over the cap is rejected before the body is read.
    declared_length: int | None = None
    raw_content_length = request.headers.get("content-length")
    if raw_content_length is not None:
        try:
            declared_length = int(raw_content_length.strip())
            if declared_length < 0:
                raise ValueError
        except (TypeError, ValueError):
            return _error_response(
                400,
                "invalid_request",
                "Invalid Content-Length header.",
            )
        if declared_length > settings.max_body_bytes:
            return _error_response(
                413,
                "payload_too_large",
                "Request body exceeds the maximum size.",
                close_connection=True,
            )

    # 5. Read the body with a streaming cap.
    chunks: list[bytes] = []
    total_bytes = 0
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > settings.max_body_bytes:
                chunks.clear()
                return _error_response(
                    413,
                    "payload_too_large",
                    "Request body exceeds the maximum size.",
                    close_connection=True,
                )
            chunks.append(chunk)
    except ClientDisconnect:
        return _error_response(
            400,
            "incomplete_body",
            "Request body ended before it was complete.",
        )

    if declared_length is not None and total_bytes < declared_length:
        return _error_response(
            400,
            "incomplete_body",
            "Request body ended before it was complete.",
        )

    body = b"".join(chunks)
    receive_ms = _elapsed_ms(receive_started)

    # 6. Empty body.
    if not body:
        return _error_response(
            400, "empty_body", "Request body is empty."
        )

    # 7. WAV structure, format and duration.
    validate_started = time.perf_counter()
    try:
        wav_info = parse_wav(
            body, max_duration_seconds=settings.max_duration_seconds
        )
    except WavError as exc:
        code = exc.code if exc.code in _WAV_CODES else "invalid_wav"
        return _error_response(400, code, exc.message)
    except Exception:
        logger.error("invalid_wav type=unexpected")
        return _error_response(
            400, "invalid_wav", "WAV data could not be parsed."
        )
    validate_ms = _elapsed_ms(validate_started)

    # 8. Storage. The app returns a 201 only after the write completes.
    recording_id = new_recording_id()
    store_started = time.perf_counter()
    try:
        stored_path = store.save(recording_id, body)
    except OSError:
        logger.warning(
            "storage_error bytes=%d", len(body)
        )
        return _error_response(
            500, "storage_error", "Could not store recording."
        )
    except Exception as exc:
        logger.error("internal_error type=%s", type(exc).__name__)
        return _error_response(
            500, "internal_error", "An internal error occurred."
        )

    # 8b. One ask, one trace. The trace is created only after the audio is on
    # disk, so a rejected upload never leaves a trace behind. The three stages
    # that already ran are recorded with the durations measured above.
    _trace_start(trace_store, recording_id)
    _record_stage(
        trace_store,
        recording_id,
        "receive",
        receive_ms,
        {"bytes": len(body), "header_bytes": header_bytes},
    )
    _record_stage(
        trace_store,
        recording_id,
        "validate",
        validate_ms,
        {"duration_seconds": wav_info.duration_seconds},
    )
    _record_stage(
        trace_store,
        recording_id,
        "store",
        _elapsed_ms(store_started),
        {"bytes": len(body)},
    )

    # 8c. Context sidecar and resolution. Neither can change the response.
    context_started = time.perf_counter()
    parsed_context, rejected_reason = decode_header(raw_context)
    document = _context_document(parsed_context, rejected_reason, header_bytes)
    if document is not None:
        try:
            write_context(stored_path, document)
        except OSError:
            logger.warning("context_error id=%s", recording_id)

    _trace_set(
        trace_store,
        recording_id,
        "context",
        {
            "header_bytes": header_bytes,
            "parsed": parsed_context if parsed_context is not None else {},
            "rejected_reason": rejected_reason,
        },
    )
    _record_stage(
        trace_store,
        recording_id,
        "context",
        _elapsed_ms(context_started),
        {"header_bytes": header_bytes, "reason": rejected_reason},
        outcome="rejected" if rejected_reason is not None else "ok",
    )

    context_block: str | None = None
    book_search: Callable[..., list] | None = None
    if parsed_context is not None:
        context_block, book_search = _record_resolve(
            trace_store, recording_id, parsed_context, resolve
        )

    stored_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    payload = {
        "id": recording_id,
        "status": "stored",
        "received_bytes": len(body),
        "duration_seconds": wav_info.duration_seconds,
        "sample_rate_hz": wav_info.sample_rate_hz,
        "channels": wav_info.channels,
        "bits_per_sample": wav_info.bits_per_sample,
        "sha256": hashlib.sha256(body).hexdigest(),
        "stored_at": stored_at,
    }
    logger.info(
        "stored id=%s bytes=%d duration=%.3f",
        recording_id,
        len(body),
        wav_info.duration_seconds,
    )

    # 9. Transcription, when it is configured. The task runs after the response
    # is sent, so it cannot change the status or the body. A failure is logged
    # by type only, never with audio or credentials. The task is the same
    # pipeline closure that a dev ask runs.
    task = pipeline if pipeline is not None else _null_pipeline
    if transcribe is not None and background is not None:
        background.add_task(
            task,
            stored_path,
            origin=ORIGIN_KINDLE,
            recording_id=recording_id,
            context_block=context_block,
            book_search=book_search,
        )
    else:
        _trace_outcome(trace_store, recording_id, "ok")

    return _json_response(payload, 201)


def _null_pipeline(
    path: pathlib.Path,
    *,
    origin: str = ORIGIN_KINDLE,
    context: Mapping[str, Any] | None = None,
    question: str | None = None,
    prompt: str | None = None,
    model: str | None = None,
    recording_id: str | None = None,
    context_block: str | None = None,
    book_search: Callable[..., list] | None = None,
) -> None:
    """Fallback task used only when ``_handle_upload`` gets no pipeline.

    It records nothing and answers nothing, so a caller that wires the handler
    by hand cannot leave a trace pending.
    """

    return None


def _dev_transcriber(question: str) -> Callable[..., dict[str, Any]]:
    """Return the transcription stand-in of a dev ask.

    A dev ask carries typed text instead of speech, so there is nothing to
    transcribe. The stage is still recorded with outcome ``skipped``, and the
    answer then runs exactly as it does after a real transcript.
    """

    def transcribe(
        path: pathlib.Path,
        trace: str | None = None,
        trace_store: TraceStore | None = None,
    ) -> dict[str, Any]:
        if trace is not None and trace_store is not None:
            _record_stage(
                trace_store,
                trace,
                "transcribe",
                detail={"source": "dev-question", "chars": len(question)},
                outcome="skipped",
            )
        return {"status": "ok", "text": question}

    return transcribe


def _record_resolve(
    trace_store: TraceStore | None,
    recording_id: str,
    context: dict,
    resolve: Callable[[dict], object] | None,
) -> tuple[str | None, Callable[..., list] | None]:
    """Resolve one context and record its stages.

    Returns the prompt block and, when the resolver named a book, the search
    callable that was built from the same loaded text.
    """

    started = time.perf_counter()
    passage, rung, reason, book_key, match, search = _resolve_context(
        context, resolve
    )
    _record_stage(
        trace_store,
        recording_id,
        "resolve",
        _elapsed_ms(started),
        {"rung": rung, "reason": reason},
    )
    _trace_set(
        trace_store,
        recording_id,
        "resolve",
        {"rung": rung, "reason": reason, "passage": passage or ""},
    )
    _trace_set(
        trace_store,
        recording_id,
        "book",
        {"key": book_key or "", "match": match},
    )
    return _context_block(context, passage), search


def _run_pipeline(
    transcribe: Callable[..., object],
    answer: Callable[..., object] | None,
    path: pathlib.Path,
    *,
    trace: str | None = None,
    trace_store: TraceStore | None = None,
    context_block: str | None = None,
    book_search: Callable[..., list] | None = None,
    prompt: str | None = None,
    model: str | None = None,
) -> None:
    """Run one transcription, then its answer, without reaching the upload path.

    The trace id, the trace store, the context block and the book search are
    passed to whichever of the two callables declares them, so a plain
    ``transcribe(path)`` still works. ``prompt`` and ``model`` are answer
    overrides from the dev ask form; an answer callable that does not declare
    them is called as before.
    """

    optional = {"trace": trace, "trace_store": trace_store}
    try:
        result = transcribe(path, **_supported_keywords(transcribe, optional))
    except Exception as exc:
        logger.warning("transcribe_error type=%s", type(exc).__name__)
        _trace_outcome(trace_store, trace, "failed")
        return

    text = result.get("text") if isinstance(result, dict) else None
    transcribed = isinstance(text, str) and bool(text.strip())
    failed = isinstance(result, dict) and result.get("status") == "error"
    if answer is None or not transcribed:
        _trace_outcome(trace_store, trace, "failed" if failed else "ok")
        return
    _answer_quietly(
        answer,
        path,
        text,
        trace=trace,
        trace_store=trace_store,
        context_block=context_block,
        book_search=book_search,
        prompt=prompt,
        model=model,
    )


def _answer_quietly(
    answer: Callable[..., object],
    path: pathlib.Path,
    transcript: str,
    *,
    trace: str | None = None,
    trace_store: TraceStore | None = None,
    context_block: str | None = None,
    book_search: Callable[..., list] | None = None,
    prompt: str | None = None,
    model: str | None = None,
) -> None:
    """Run one answer without letting it reach the upload path."""

    optional = {
        "trace": trace,
        "trace_store": trace_store,
        "context_block": context_block,
        "book_search": book_search,
    }
    if prompt is not None:
        optional["prompt"] = prompt
    if model is not None:
        optional["model"] = model
    try:
        answer(path, transcript, **_supported_keywords(answer, optional))
    except Exception as exc:
        logger.warning("answer_error type=%s", type(exc).__name__)
        _trace_outcome(trace_store, trace, "failed")
        return
    _trace_outcome(trace_store, trace, "ok")


def _supported_keywords(
    callable_: Callable[..., object], candidates: Mapping[str, Any]
) -> dict[str, Any]:
    """Return the subset of ``candidates`` that ``callable_`` accepts.

    The receiver's injection points existed before the trace and the reading
    context, so a callable that does not name them is called as before.
    """

    try:
        parameters = inspect.signature(callable_).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return {}
    accepts_extra = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    return {
        name: value
        for name, value in candidates.items()
        if accepts_extra or name in parameters
    }


def _resolve_context(
    parsed_context: dict, resolve: Callable[[dict], object] | None
) -> tuple[str | None, str, str, str | None, str, Callable[..., list] | None]:
    """Resolve one context to ``(passage, rung, reason, key, match, search)``.

    A resolver may return the documented five values or a sixth, the book
    search callable, so an older resolver keeps working. Without a resolver
    there is no library to search. A resolver that returns an unexpected shape
    or raises is recorded as an unresolved ask, never as a failed upload.
    """

    if resolve is None:
        return None, "none", "no-library", None, "none", None
    try:
        result = resolve(parsed_context)
    except Exception as exc:
        logger.warning("resolve_error type=%s", type(exc).__name__)
        return None, "none", "resolve-failed", None, "none", None
    if not isinstance(result, tuple) or len(result) not in (5, 6):
        logger.warning("resolve_error type=shape")
        return None, "none", "bad-result", None, "none", None

    passage, rung, reason, book_key, match = result[:5]
    search = result[5] if len(result) == 6 else None
    return (
        passage if isinstance(passage, str) and passage else None,
        rung if isinstance(rung, str) and rung else "none",
        reason if isinstance(reason, str) else "",
        book_key if isinstance(book_key, str) and book_key else None,
        match if isinstance(match, str) and match else "none",
        search if callable(search) else None,
    )


def _library_resolver(library: Library) -> Callable[[dict], tuple]:
    """Return a resolver backed by one synced-book library.

    The resolver returns the passage and the book search callable that was
    built from the same loaded text, so an ask never reads the book twice.
    """

    def resolve(parsed_context: dict) -> tuple:
        book = (
            parsed_context.get("book")
            if isinstance(parsed_context, dict)
            else None
        )
        if not isinstance(book, dict):
            return None, "none", "no-book", None, "none", None
        key = library.find(
            str(book.get("title") or ""),
            book.get("authors") or "",
            size_bytes=book.get("size_bytes"),
            partial_md5=book.get("partial_md5"),
        )
        if key is None:
            return None, "none", "no-book", None, "none", None
        loaded = library.load(key) or {}
        text = loaded.get("text") or {}
        passage, rung, reason = resolve_passage(parsed_context, text)
        aliases = (loaded.get("meta") or {}).get("aliases") or []
        md5 = book.get("partial_md5")
        match = "md5-alias" if md5 and md5 in aliases else "title+size"
        search = book_search(text, fraction=_fraction(parsed_context))
        return passage, rung, reason, key, match, search

    return resolve


def _fraction(parsed_context: object) -> float | None:
    """Return the reading fraction of one context payload, or ``None``."""

    pos = parsed_context.get("pos") if isinstance(parsed_context, dict) else None
    value = pos.get("fraction") if isinstance(pos, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _context_block(parsed_context: dict | None, passage: str | None) -> str | None:
    """Build the prompt block from the context, with the resolved passage.

    A resolved passage takes the place of the highlight. Returns ``None`` when
    the ask carried no context or the context has neither a title nor a passage,
    so a context-free ask keeps its old messages.
    """

    if not isinstance(parsed_context, dict):
        return None
    context = {**parsed_context, "highlight": passage} if passage else parsed_context
    return build_context_block(context)


def _context_document(
    parsed_context: dict | None, rejected_reason: str | None, header_bytes: int
) -> dict | None:
    """Return the ``.context.json`` document for one header, or ``None``.

    ``None`` means the header was absent, so no sidecar is written.
    """

    if parsed_context is not None:
        return {
            "schema": CONTEXT_SCHEMA,
            "status": "ok",
            "parsed": parsed_context,
            "header_bytes": header_bytes,
        }
    if rejected_reason is not None:
        return {
            "schema": CONTEXT_SCHEMA,
            "status": "invalid",
            "reason": rejected_reason,
        }
    return None


def context_path(wav_path: pathlib.Path) -> pathlib.Path:
    """Return the ``.context.json`` sidecar path for one recording."""

    return wav_path.with_suffix(".context.json")


def write_context(wav_path: pathlib.Path, data: dict) -> None:
    """Write the context sidecar atomically with mode 0600. Never touches the WAV."""

    path = context_path(wav_path)
    body = json.dumps(data, indent=2, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=".context-", suffix=".part"
    )
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _trace_start(trace_store: TraceStore | None, recording_id: str) -> None:
    """Start the trace of one ask. A trace failure never fails the upload."""

    if trace_store is None:
        return
    trace_safely(
        lambda: trace_store.start(recording_id, origin=ORIGIN_KINDLE)
    )


def _trace_set(
    trace_store: TraceStore | None, recording_id: str, section: str, value: dict
) -> None:
    """Replace one trace section, when a trace store is configured."""

    if trace_store is None:
        return
    trace_safely(lambda: trace_store.set(recording_id, section, value))


def _trace_outcome(
    trace_store: TraceStore | None, recording_id: str | None, outcome: str
) -> None:
    """Set the outcome of one ask, when a trace store is configured."""

    if trace_store is None or recording_id is None:
        return
    trace_safely(lambda: trace_store.set_outcome(recording_id, outcome))


def _record_stage(
    trace_store: TraceStore | None,
    recording_id: str,
    name: str,
    duration_ms: float | None = None,
    detail: dict | None = None,
    *,
    outcome: str = "ok",
) -> None:
    """Append one stage to the trace of one ask, when a store is configured."""

    if trace_store is None:
        return
    trace_safely(
        lambda: trace_store.stage(
            recording_id,
            name,
            outcome=outcome,
            detail=detail,
            duration_ms=duration_ms,
        )
    )


def _elapsed_ms(started: float) -> float:
    """Milliseconds since ``started``, rounded like the trace store does."""

    return round((time.perf_counter() - started) * 1000.0, 3)


def _answer_response(recording_id: str, sidecar: dict | None) -> JSONResponse:
    """Shape one answer sidecar into the read contract."""

    if sidecar is None:
        return _json_response({"id": recording_id, "status": "pending"}, 200)
    if sidecar.get("status") == "ok":
        model = sidecar.get("model")
        return _json_response(
            {
                "id": recording_id,
                "status": "ready",
                "text": sidecar.get("text") or "",
                "model": model if isinstance(model, str) else "",
            },
            200,
        )
    error = sidecar.get("error")
    return _json_response(
        {
            "id": recording_id,
            "status": "failed",
            "error": error
            if isinstance(error, str) and error
            else "The answer failed.",
        },
        200,
    )


def _is_wav_content_type(value: str | None) -> bool:
    if not value:
        return False
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "audio/wav"


def _json_response(
    payload: dict[str, object],
    status_code: int,
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
    extra_headers: Mapping[str, str] | None = None,
    close_connection: bool = False,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    if close_connection:
        headers["Connection"] = "close"
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )


def _prepare_storage(storage_dir: pathlib.Path) -> None:
    if storage_dir.exists() and not storage_dir.is_dir():
        raise RuntimeError(
            f"Storage directory {storage_dir} is not a directory."
        )

    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Storage directory {storage_dir} could not be created."
        ) from exc

    if not storage_dir.is_dir():
        raise RuntimeError(
            f"Storage directory {storage_dir} is not a directory."
        )

    _probe_writable_directory(storage_dir)


def _probe_writable_directory(storage_dir: pathlib.Path) -> None:
    """Prove ``storage_dir`` is writable without touching existing files.

    ``tempfile`` creates the probe under a unique name with exclusive creation,
    so an existing file is never truncated, overwritten or reused. Only the file
    this call created is removed.
    """

    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=storage_dir,
            prefix=_WRITE_PROBE_PREFIX,
            delete=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"Storage directory {storage_dir} is not writable."
        ) from exc

    probe_path = pathlib.Path(handle.name)
    try:
        with handle:
            handle.write(b"kvb")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RuntimeError(
            f"Storage directory {storage_dir} is not writable."
        ) from exc
    finally:
        try:
            probe_path.unlink()
        except OSError:
            pass


def _required_env(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is not set")
    return value


def _optional_int(environ: Mapping[str, str], name: str) -> int | None:
    raw_value = environ.get(name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return int(raw_value.strip(), 10)
    except ValueError:
        raise RuntimeError(
            f"{name} must be a base-10 integer"
        ) from None


def _optional_float(environ: Mapping[str, str], name: str) -> float | None:
    raw_value = environ.get(name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return float(raw_value.strip())
    except ValueError:
        raise RuntimeError(
            f"{name} must be a number"
        ) from None


def _optional_bool(
    environ: Mapping[str, str], name: str, default: bool
) -> bool:
    """Return the boolean in ``name``, or ``default`` when it is unset.

    ``0``, ``false``, ``no`` and ``off`` are false; every other value is true.
    """

    raw_value = environ.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}
