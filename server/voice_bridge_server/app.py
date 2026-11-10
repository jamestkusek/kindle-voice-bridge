"""FastAPI application for the Kindle voice-bridge receiver.

The app implements the v1 contract in ``docs/protocol.md``: one authenticated
upload endpoint, an unauthenticated health probe, bounded body reads, structural
WAV validation and atomic local storage. It never logs the token, the
``Authorization`` header or audio bytes, and it never retries.

Binding and transport (uvicorn ``--host``/``--port``, TLS termination) are
outside this module. ``create_app`` never reads the environment; use
:func:`create_app_from_env` for the uvicorn factory.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from .audio import WavError, parse_wav
from .config import Settings
from .storage import LocalFileStore, RecordingStore, new_recording_id

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
_WRITE_PROBE_NAME = ".kvb-write-probe"


def create_app(
    settings: Settings,
    *,
    store: RecordingStore | None = None,
) -> FastAPI:
    """Build the ASGI application and validate the storage directory."""

    _prepare_storage(settings.storage_dir)

    app = FastAPI(
        title="Kindle Voice Bridge",
        version=_API_VERSION,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    # ``redirect_slashes`` is also set on the router so POST bodies are never
    # answered with a 307 redirect, regardless of FastAPI version details.
    app.router.redirect_slashes = False

    app.state.settings = settings
    app.state.store = (
        store if store is not None else LocalFileStore(settings.storage_dir)
    )

    @app.post("/v1/recordings", include_in_schema=False)
    @app.post("/v1/recordings/", include_in_schema=False)
    async def upload_recording(request: Request) -> Response:
        return await _handle_upload(request, settings, app.state.store)

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

    Used by ``uvicorn --factory``. Missing or blank required variables raise
    ``RuntimeError`` naming the variable only; invalid values surface as
    ``ValueError`` from :class:`Settings`.
    """

    env = os.environ if environ is None else environ

    token = _required_env(env, "KVB_TOKEN")
    storage_dir = _required_env(env, "KVB_STORAGE_DIR")
    max_body_bytes = _optional_int(env, "KVB_MAX_BODY_BYTES")
    max_duration_seconds = _optional_float(env, "KVB_MAX_DURATION_SECONDS")

    settings_kwargs: dict[str, int | float] = {}
    if max_body_bytes is not None:
        settings_kwargs["max_body_bytes"] = max_body_bytes
    if max_duration_seconds is not None:
        settings_kwargs["max_duration_seconds"] = max_duration_seconds

    settings = Settings(
        token=token,
        storage_dir=storage_dir,
        **settings_kwargs,
    )
    return create_app(settings)


async def _handle_upload(
    request: Request,
    settings: Settings,
    store: RecordingStore,
) -> Response:
    # 2. Authentication precedes every body-related check.
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) != 1 or not _token_matches(
        authorization_values[0], settings.token
    ):
        return _error_response(
            401,
            "unauthorized",
            "Missing or invalid bearer token.",
            extra_headers={"WWW-Authenticate": 'Bearer realm="voice-bridge"'},
        )

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

    # 4. Declared length over the cap is rejected without reading the body.
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

    # 6. Empty body.
    if not body:
        return _error_response(
            400, "empty_body", "Request body is empty."
        )

    # 7. WAV structure, format and duration.
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

    # 8. Storage. A 201 is returned only after the write completed.
    recording_id = new_recording_id()
    try:
        store.save(recording_id, body)
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
    return _json_response(payload, 201)


def _token_matches(value: str, expected: str) -> bool:
    parts = value.split(" ")
    if len(parts) != 2:
        return False
    scheme, token = parts
    if scheme.lower() != "bearer" or not token:
        return False
    try:
        return secrets.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        )
    except (TypeError, UnicodeEncodeError):  # pragma: no cover - defensive
        return False


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

    probe_path = storage_dir / _WRITE_PROBE_NAME
    try:
        with open(probe_path, "wb") as handle:
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
        except FileNotFoundError:
            pass
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
