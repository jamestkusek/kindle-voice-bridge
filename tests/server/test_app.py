"""HTTP contract tests for the receiver described in docs/protocol.md.

The tests use FastAPI's in-process TestClient and, where the contract depends on
ASGI receive behaviour (an oversized declared body must not be read), a direct
in-process ASGI call.  All audio is generated with the standard library in a
pytest ``tmp_path`` directory; no network, device or real recording is used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import pathlib
import re
import struct
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from voice_bridge_server.app import create_app, create_app_from_env
from voice_bridge_server.config import Settings

TOKEN = "test-token-0123456789abcdef"
URL = "/v1/recordings"
RECORDING_ID_RE = re.compile(r"^rec_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# WAV builders (standard library only)
# ---------------------------------------------------------------------------


def _chunk(chunk_id: bytes, payload: bytes) -> bytes:
    if len(chunk_id) != 4:
        raise ValueError("chunk id must be four bytes")
    padding = b"\x00" if len(payload) % 2 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def _fmt_payload(
    *,
    rate: int = 8000,
    bits: int = 16,
    channels: int = 1,
    audio_format: int = 1,
    block_align: int | None = None,
    byte_rate: int | None = None,
    extra: bytes = b"",
) -> bytes:
    align = channels * bits // 8 if block_align is None else block_align
    computed_byte_rate = rate * align if byte_rate is None else byte_rate
    return (
        struct.pack(
            "<HHIIHH",
            audio_format,
            channels,
            rate,
            computed_byte_rate,
            align,
            bits,
        )
        + extra
    )


def _riff(
    chunks: list[bytes],
    *,
    riff: bytes = b"RIFF",
    wave_form: bytes = b"WAVE",
    riff_size: int | None = None,
) -> bytes:
    payload = wave_form + b"".join(chunks)
    size = len(payload) if riff_size is None else riff_size
    return riff + struct.pack("<I", size) + payload


def _wav(
    *,
    rate: int = 8000,
    bits: int = 16,
    channels: int = 1,
    frames: int = 100,
    data: bytes | None = None,
    audio_format: int = 1,
    extra_chunks: tuple[bytes, ...] = (),
    fmt_extra: bytes = b"",
    block_align: int | None = None,
    byte_rate: int | None = None,
    trailing: bytes = b"",
) -> bytes:
    align = channels * bits // 8 if block_align is None else block_align
    if data is None:
        data = b"\x00" * (frames * align)
    fmt = _fmt_payload(
        rate=rate,
        bits=bits,
        channels=channels,
        audio_format=audio_format,
        block_align=block_align,
        byte_rate=byte_rate,
        extra=fmt_extra,
    )
    chunks = [_chunk(b"fmt ", fmt), *extra_chunks, _chunk(b"data", data)]
    return _riff(chunks) + trailing


def _extensible_fmt() -> bytes:
    pcm_subformat = (
        b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    )
    extra = (
        struct.pack("<H", 22)
        + struct.pack("<H", 16)
        + struct.pack("<I", 0)
        + pcm_subformat
    )
    return _fmt_payload(audio_format=0xFFFE, extra=extra)


def _headers(
    *,
    auth: str | None = f"Bearer {TOKEN}",
    content_type: str | None = "audio/wav",
    **extra: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if auth is not None:
        headers["Authorization"] = auth
    if content_type is not None:
        headers["Content-Type"] = content_type
    headers.update(extra)
    return headers


def _assert_error(
    response,
    status: int,
    code: str,
    *,
    storage_dir: pathlib.Path | None = None,
) -> dict:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers.get("cache-control") == "no-store"
    payload = response.json()
    assert "error" in payload
    error = payload["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert TOKEN not in response.text
    if storage_dir is not None:
        assert str(storage_dir) not in response.text
    return error


def _assert_no_artifacts(storage_dir: pathlib.Path) -> None:
    assert storage_dir.exists()
    assert list(storage_dir.iterdir()) == []


def _stored_wavs(storage_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(storage_dir.glob("*.wav"))


class SpyStore:
    """A RecordingStore that records arguments and writes the final file."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.calls: list[tuple[str, bytes]] = []

    def save(self, recording_id: str, body: bytes) -> pathlib.Path:
        self.calls.append((recording_id, body))
        path = self.root / f"{recording_id}.wav"
        path.write_bytes(body)
        return path


class FailingStore:
    """A RecordingStore that always fails, like a full or read-only disk."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def save(self, recording_id: str, body: bytes) -> pathlib.Path:
        self.calls.append((recording_id, body))
        raise OSError("simulated disk full")


def _asgi_post(
    app,
    *,
    headers: list[tuple[str, str]],
    chunks: list[bytes],
    path: str = URL,
) -> tuple[int, dict[str, str], bytes, int]:
    """Send one in-process ASGI POST and return status, headers, body, receives."""
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks)
    ]
    if not messages:
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    asyncio.run(app(scope, receive, send))

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    return start["status"], response_headers, body, receive_count


def _log_texts(caplog: pytest.LogCaptureFixture) -> str:
    texts: list[str] = []
    for record in caplog.records:
        if record.name.startswith(("httpx", "httpcore")):
            continue
        texts.append(record.getMessage())
        if record.exc_text:
            texts.append(record.exc_text)
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# Fixtures and shared bodies
# ---------------------------------------------------------------------------


@pytest.fixture
def storage_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "recordings"


@pytest.fixture
def settings(storage_dir: pathlib.Path) -> Settings:
    return Settings(token=TOKEN, storage_dir=storage_dir)


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _malformed_bodies() -> list[tuple[str, bytes]]:
    fmt = _fmt_payload()
    data = b"\x00" * 16
    valid = _wav(frames=100)
    return [
        ("truncated-mid-data", valid[:-10]),
        ("bad-riff-magic", b"XXXX" + valid[4:]),
        ("rifx", b"RIFX" + valid[4:]),
        ("rf64", b"RF64" + valid[4:]),
        (
            "data-size-not-multiple-of-block-align",
            _riff([_chunk(b"fmt ", fmt), _chunk(b"data", b"\x00\x00\x00")]),
        ),
        (
            "duplicate-fmt",
            _riff([_chunk(b"fmt ", fmt), _chunk(b"fmt ", fmt), _chunk(b"data", data)]),
        ),
        ("missing-data", _riff([_chunk(b"fmt ", fmt)])),
        ("data-before-fmt", _riff([_chunk(b"data", data), _chunk(b"fmt ", fmt)])),
        (
            "chunk-overruns-body",
            _riff(
                [
                    _chunk(b"fmt ", fmt),
                    b"data" + struct.pack("<I", 1000) + b"\x00" * 10,
                ]
            ),
        ),
        ("shorter-than-44-bytes", b"RIFF" + struct.pack("<I", 4) + b"WAVE"),
    ]


MALFORMED_BODIES = _malformed_bodies()

UNSUPPORTED_FORMAT_BODIES = [
    ("stereo", _wav(channels=2, frames=10)),
    ("24-bit", _wav(bits=24, frames=10)),
    ("32-bit", _wav(bits=32, frames=10)),
    ("4-khz", _wav(rate=4000, frames=10)),
    ("96-khz", _wav(rate=96000, frames=10)),
    ("ieee-float", _wav(audio_format=3, frames=10)),
    (
        "extensible",
        _riff([_chunk(b"fmt ", _extensible_fmt()), _chunk(b"data", b"\x00" * 16)]),
    ),
]


# ---------------------------------------------------------------------------
# Success, bytes and acknowledgement
# ---------------------------------------------------------------------------


def test_app_exposes_settings_and_store(app, settings: Settings) -> None:
    assert app.state.settings is settings
    assert app.state.store is not None


def test_create_app_creates_storage_and_removes_the_probe(
    tmp_path: pathlib.Path,
) -> None:
    storage = tmp_path / "new" / "recordings"

    create_app(Settings(token=TOKEN, storage_dir=storage))

    assert storage.is_dir()
    assert not (storage / ".kvb-write-probe").exists()


def test_valid_upload_returns_acknowledgment_and_stores_exact_bytes(
    client: TestClient,
    settings: Settings,
) -> None:
    body = _wav(frames=8000)

    response = client.post(URL, content=body, headers=_headers())

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers.get("cache-control") == "no-store"
    assert "location" not in {name.lower() for name in response.headers}
    payload = response.json()
    assert payload["status"] == "stored"
    assert payload["received_bytes"] == len(body)
    assert payload["duration_seconds"] == 1.0
    assert payload["sample_rate_hz"] == 8000
    assert payload["channels"] == 1
    assert payload["bits_per_sample"] == 16
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()
    assert RECORDING_ID_RE.fullmatch(payload["id"])
    assert datetime.fromisoformat(payload["stored_at"]).tzinfo is not None
    assert TOKEN not in response.text
    assert str(settings.storage_dir) not in response.text

    stored = settings.storage_dir / f"{payload['id']}.wav"
    assert stored.read_bytes() == body
    assert _stored_wavs(settings.storage_dir) == [stored]


def test_two_uploads_get_distinct_ids_and_files(
    client: TestClient,
    settings: Settings,
) -> None:
    first_body = _wav(frames=100)
    second_body = _wav(frames=200)

    first = client.post(URL, content=first_body, headers=_headers())
    second = client.post(URL, content=second_body, headers=_headers())

    assert first.status_code == 201
    assert second.status_code == 201
    first_id = first.json()["id"]
    second_id = second.json()["id"]
    assert first_id != second_id
    assert (settings.storage_dir / f"{first_id}.wav").read_bytes() == first_body
    assert (settings.storage_dir / f"{second_id}.wav").read_bytes() == second_body
    assert len(_stored_wavs(settings.storage_dir)) == 2


def test_store_seam_receives_exact_bytes_and_generated_id(
    settings: Settings,
) -> None:
    store = SpyStore(settings.storage_dir)
    app = create_app(settings, store=store)

    with TestClient(app) as test_client:
        body = _wav(frames=100)
        response = test_client.post(URL, content=body, headers=_headers())

    assert response.status_code == 201
    assert len(store.calls) == 1
    recording_id, received = store.calls[0]
    assert recording_id == response.json()["id"]
    assert RECORDING_ID_RE.fullmatch(recording_id)
    assert received == body


def test_trailing_slash_routes_without_a_redirect(client: TestClient) -> None:
    response = client.post(
        URL + "/",
        content=_wav(frames=100),
        headers=_headers(),
        follow_redirects=False,
    )

    assert response.status_code == 201


def test_body_exactly_at_the_size_cap_is_accepted(tmp_path: pathlib.Path) -> None:
    body = _wav(frames=100)
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "exact-cap",
        max_body_bytes=len(body),
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.post(URL, content=body, headers=_headers())

    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "Bearer wrong-token",
        f"Basic {TOKEN}",
        "Bearer",
        f"Bearer  {TOKEN}",
        f"Bearer\t{TOKEN}",
        f"Bearer {TOKEN.upper()}",
    ],
    ids=[
        "missing",
        "wrong",
        "basic",
        "empty-bearer",
        "two-spaces",
        "tab",
        "wrong-case-token",
    ],
)
def test_auth_rejections(
    client: TestClient,
    settings: Settings,
    auth: str | None,
) -> None:
    response = client.post(URL, content=_wav(frames=100), headers=_headers(auth=auth))

    error = _assert_error(
        response,
        401,
        "unauthorized",
        storage_dir=settings.storage_dir,
    )
    assert error["message"] == "Missing or invalid bearer token."
    assert response.headers["www-authenticate"] == 'Bearer realm="voice-bridge"'
    _assert_no_artifacts(settings.storage_dir)


def test_duplicate_authorization_header_is_rejected(
    client: TestClient,
    settings: Settings,
) -> None:
    headers = [
        ("Authorization", f"Bearer {TOKEN}"),
        ("Authorization", f"Bearer {TOKEN}"),
        ("Content-Type", "audio/wav"),
    ]

    response = client.post(URL, content=_wav(frames=100), headers=headers)

    _assert_error(response, 401, "unauthorized", storage_dir=settings.storage_dir)
    _assert_no_artifacts(settings.storage_dir)


def test_lowercase_bearer_scheme_is_accepted(client: TestClient) -> None:
    response = client.post(
        URL,
        content=_wav(frames=100),
        headers=_headers(auth=f"bearer {TOKEN}"),
    )

    assert response.status_code == 201


def test_auth_is_checked_before_content_type(
    client: TestClient,
    settings: Settings,
) -> None:
    response = client.post(
        URL,
        content=b"not audio",
        headers=_headers(auth=None, content_type="audio/mpeg"),
    )

    _assert_error(response, 401, "unauthorized", storage_dir=settings.storage_dir)


def test_auth_is_checked_before_body_limits(tmp_path: pathlib.Path) -> None:
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "auth-first",
        max_body_bytes=8,
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.post(
            URL,
            content=b"x" * 100,
            headers=_headers(auth=None),
        )

    _assert_error(response, 401, "unauthorized", storage_dir=settings.storage_dir)
    _assert_no_artifacts(settings.storage_dir)


# ---------------------------------------------------------------------------
# Media type and content encoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        "audio/mpeg",
        "multipart/form-data; boundary=x",
        "application/octet-stream",
        "audio/wave",
        "audio/x-wav",
        None,
    ],
    ids=["mpeg", "multipart", "octet-stream", "wave-alias", "x-wav", "missing"],
)
def test_unsupported_media_type_rejected(
    client: TestClient,
    settings: Settings,
    content_type: str | None,
) -> None:
    response = client.post(
        URL,
        content=_wav(frames=100),
        headers=_headers(content_type=content_type),
    )

    _assert_error(
        response,
        415,
        "unsupported_media_type",
        storage_dir=settings.storage_dir,
    )
    _assert_no_artifacts(settings.storage_dir)


@pytest.mark.parametrize(
    "content_type",
    ["audio/wav; charset=binary", "Audio/WAV", "audio/wav; codecs=pcm"],
)
def test_content_type_parameters_and_case_accepted(
    client: TestClient,
    content_type: str,
) -> None:
    response = client.post(
        URL,
        content=_wav(frames=100),
        headers=_headers(content_type=content_type),
    )

    assert response.status_code == 201


def test_content_encoding_gzip_is_rejected(
    client: TestClient,
    settings: Settings,
) -> None:
    response = client.post(
        URL,
        content=_wav(frames=100),
        headers=_headers(**{"Content-Encoding": "gzip"}),
    )

    _assert_error(
        response,
        415,
        "unsupported_media_type",
        storage_dir=settings.storage_dir,
    )
    _assert_no_artifacts(settings.storage_dir)


def test_content_encoding_identity_is_accepted(client: TestClient) -> None:
    response = client.post(
        URL,
        content=_wav(frames=100),
        headers=_headers(**{"Content-Encoding": "identity"}),
    )

    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Request size limits, fixed-length and streamed
# ---------------------------------------------------------------------------


def test_declared_oversized_body_is_rejected(tmp_path: pathlib.Path) -> None:
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "declared-oversize",
        max_body_bytes=1024,
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.post(
            URL,
            content=b"\x00" * 1025,
            headers=_headers(),
        )

    _assert_error(
        response,
        413,
        "payload_too_large",
        storage_dir=settings.storage_dir,
    )
    _assert_no_artifacts(settings.storage_dir)


def test_declared_oversized_body_is_not_read(tmp_path: pathlib.Path) -> None:
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "not-read",
        max_body_bytes=1024,
    )
    store = SpyStore(settings.storage_dir)
    app = create_app(settings, store=store)

    status, headers, body, receive_count = _asgi_post(
        app,
        headers=[
            ("Authorization", f"Bearer {TOKEN}"),
            ("Content-Type", "audio/wav"),
            ("Content-Length", "1025"),
        ],
        chunks=[],
    )

    assert status == 413
    assert headers.get("content-type", "").startswith("application/json")
    assert headers.get("cache-control") == "no-store"
    assert json.loads(body)["error"]["code"] == "payload_too_large"
    assert receive_count == 0
    assert store.calls == []
    _assert_no_artifacts(settings.storage_dir)


def test_streaming_oversized_body_without_content_length_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "streaming-oversize",
        max_body_bytes=1024,
    )
    store = SpyStore(settings.storage_dir)
    app = create_app(settings, store=store)
    chunks = [b"\x00" * 200 for _ in range(10)]

    status, headers, body, receive_count = _asgi_post(
        app,
        headers=[
            ("Authorization", f"Bearer {TOKEN}"),
            ("Content-Type", "audio/wav"),
        ],
        chunks=chunks,
    )

    assert status == 413
    assert headers.get("content-type", "").startswith("application/json")
    assert headers.get("cache-control") == "no-store"
    assert headers.get("connection") == "close"
    assert json.loads(body)["error"]["code"] == "payload_too_large"
    assert 0 < receive_count < len(chunks)
    assert store.calls == []
    _assert_no_artifacts(settings.storage_dir)


# ---------------------------------------------------------------------------
# WAV structure, format and duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,body",
    MALFORMED_BODIES,
    ids=[name for name, _ in MALFORMED_BODIES],
)
def test_malformed_or_truncated_wav_is_rejected(
    client: TestClient,
    settings: Settings,
    name: str,
    body: bytes,
) -> None:
    response = client.post(URL, content=body, headers=_headers())

    _assert_error(response, 400, "invalid_wav", storage_dir=settings.storage_dir)
    _assert_no_artifacts(settings.storage_dir)


def test_empty_body_is_rejected(client: TestClient, settings: Settings) -> None:
    response = client.post(URL, content=b"", headers=_headers())

    _assert_error(response, 400, "empty_body", storage_dir=settings.storage_dir)
    _assert_no_artifacts(settings.storage_dir)


def test_failed_upload_leaves_existing_recordings_untouched(
    client: TestClient,
    settings: Settings,
) -> None:
    first = client.post(URL, content=_wav(frames=100), headers=_headers())
    assert first.status_code == 201
    stored = settings.storage_dir / f"{first.json()['id']}.wav"
    original = stored.read_bytes()

    second = client.post(
        URL,
        content=_wav(frames=100)[:-10],
        headers=_headers(),
    )

    _assert_error(second, 400, "invalid_wav", storage_dir=settings.storage_dir)
    assert stored.exists()
    assert stored.read_bytes() == original
    assert _stored_wavs(settings.storage_dir) == [stored]


@pytest.mark.parametrize(
    "name,body",
    UNSUPPORTED_FORMAT_BODIES,
    ids=[name for name, _ in UNSUPPORTED_FORMAT_BODIES],
)
def test_unsupported_wav_format_is_rejected(
    client: TestClient,
    settings: Settings,
    name: str,
    body: bytes,
) -> None:
    response = client.post(URL, content=body, headers=_headers())

    _assert_error(
        response,
        400,
        "unsupported_wav_format",
        storage_dir=settings.storage_dir,
    )
    _assert_no_artifacts(settings.storage_dir)


@pytest.mark.parametrize(
    ("rate", "bits"),
    [
        (8000, 8),
        (8000, 16),
        (16000, 16),
        (48000, 8),
        (48000, 16),
    ],
)
def test_supported_formats_are_accepted(
    tmp_path: pathlib.Path,
    rate: int,
    bits: int,
) -> None:
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / f"format-{rate}-{bits}",
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        body = _wav(rate=rate, bits=bits, frames=max(1, rate // 100))
        response = test_client.post(URL, content=body, headers=_headers())

    assert response.status_code == 201
    payload = response.json()
    assert payload["sample_rate_hz"] == rate
    assert payload["bits_per_sample"] == bits
    assert payload["channels"] == 1


def test_exactly_fifteen_seconds_is_accepted(client: TestClient) -> None:
    body = _wav(rate=8000, bits=16, frames=8000 * 15)
    assert len(body) == 240044

    response = client.post(URL, content=body, headers=_headers())

    assert response.status_code == 201
    assert response.json()["duration_seconds"] == 15.0


def test_one_frame_over_fifteen_seconds_is_rejected(
    client: TestClient,
    settings: Settings,
) -> None:
    body = _wav(rate=8000, bits=16, data=b"\x00" * (8000 * 15 * 2 + 2))

    response = client.post(URL, content=body, headers=_headers())

    _assert_error(
        response,
        400,
        "duration_exceeds_limit",
        storage_dir=settings.storage_dir,
    )
    _assert_no_artifacts(settings.storage_dir)


def test_custom_duration_limit_is_enforced(tmp_path: pathlib.Path) -> None:
    settings = Settings(
        token=TOKEN,
        storage_dir=tmp_path / "custom-duration",
        max_duration_seconds=1.0,
    )
    app = create_app(settings)

    with TestClient(app) as test_client:
        response = test_client.post(
            URL,
            content=_wav(frames=8001),
            headers=_headers(),
        )

    _assert_error(
        response,
        400,
        "duration_exceeds_limit",
        storage_dir=settings.storage_dir,
    )
    _assert_no_artifacts(settings.storage_dir)


# ---------------------------------------------------------------------------
# Storage failures
# ---------------------------------------------------------------------------


def test_storage_failure_returns_500_and_leaves_no_artifacts(
    settings: Settings,
) -> None:
    store = FailingStore()
    app = create_app(settings, store=store)

    with TestClient(app) as test_client:
        body = _wav(frames=100)
        response = test_client.post(URL, content=body, headers=_headers())

    _assert_error(response, 500, "storage_error", storage_dir=settings.storage_dir)
    assert "simulated disk full" not in response.text
    assert "id" not in response.json()
    assert len(store.calls) == 1
    recording_id, received = store.calls[0]
    assert RECORDING_ID_RE.fullmatch(recording_id)
    assert received == body
    _assert_no_artifacts(settings.storage_dir)


# ---------------------------------------------------------------------------
# Settings and environment factory
# ---------------------------------------------------------------------------


def test_settings_defaults_are_pure(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "not-created"

    settings = Settings(token=TOKEN, storage_dir=target)

    assert settings.token == TOKEN
    assert isinstance(settings.storage_dir, pathlib.Path)
    assert settings.storage_dir == target.resolve()
    assert settings.max_body_bytes == 2 * 1024 * 1024
    assert settings.max_duration_seconds == 15.0
    assert not target.exists()


@pytest.mark.parametrize(
    "token",
    ["", "   ", "a b", "a\tb", "a\nb"],
    ids=["empty", "spaces", "space-inside", "tab", "newline"],
)
def test_settings_rejects_invalid_token(tmp_path: pathlib.Path, token: str) -> None:
    with pytest.raises(ValueError):
        Settings(token=token, storage_dir=tmp_path / "recordings")


def test_settings_rejects_empty_storage_dir() -> None:
    with pytest.raises(ValueError):
        Settings(token=TOKEN, storage_dir="")


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_settings_rejects_invalid_body_limit(tmp_path: pathlib.Path, value) -> None:
    with pytest.raises(ValueError):
        Settings(
            token=TOKEN,
            storage_dir=tmp_path / "recordings",
            max_body_bytes=value,
        )


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_settings_rejects_invalid_duration_limit(
    tmp_path: pathlib.Path,
    value,
) -> None:
    with pytest.raises(ValueError):
        Settings(
            token=TOKEN,
            storage_dir=tmp_path / "recordings",
            max_duration_seconds=value,
        )


def test_create_app_rejects_a_storage_path_that_is_a_file(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "not-a-directory"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError):
        create_app(Settings(token=TOKEN, storage_dir=path))


def test_create_app_from_env_requires_token(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RuntimeError, match="KVB_TOKEN"):
        create_app_from_env({"KVB_STORAGE_DIR": str(tmp_path / "recordings")})


def test_create_app_from_env_requires_storage_dir() -> None:
    with pytest.raises(RuntimeError, match="KVB_STORAGE_DIR"):
        create_app_from_env({"KVB_TOKEN": TOKEN})


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_create_app_from_env_rejects_blank_token(
    tmp_path: pathlib.Path,
    blank: str,
) -> None:
    with pytest.raises(RuntimeError, match="KVB_TOKEN"):
        create_app_from_env(
            {
                "KVB_TOKEN": blank,
                "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
            }
        )


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_create_app_from_env_rejects_blank_storage_dir(
    tmp_path: pathlib.Path,
    blank: str,
) -> None:
    with pytest.raises(RuntimeError, match="KVB_STORAGE_DIR"):
        create_app_from_env({"KVB_TOKEN": TOKEN, "KVB_STORAGE_DIR": blank})


def test_create_app_from_env_error_does_not_echo_the_token() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        create_app_from_env({"KVB_TOKEN": TOKEN})

    assert TOKEN not in str(excinfo.value)


def test_create_app_from_env_builds_an_app_and_creates_storage(
    tmp_path: pathlib.Path,
) -> None:
    storage = tmp_path / "env-recordings"
    environment = {
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(storage),
        "KVB_MAX_BODY_BYTES": "4096",
        "KVB_MAX_DURATION_SECONDS": "2.5",
    }

    app = create_app_from_env(environment)

    assert app.state.settings.token == TOKEN
    assert app.state.settings.storage_dir == storage.resolve()
    assert app.state.settings.max_body_bytes == 4096
    assert app.state.settings.max_duration_seconds == 2.5
    assert storage.is_dir()
    with TestClient(app) as test_client:
        health = test_client.get("/healthz")
        response = test_client.post(
            URL,
            content=_wav(frames=100),
            headers=_headers(),
        )
    assert health.status_code == 200
    assert response.status_code == 201


def test_create_app_from_env_blank_optional_values_use_defaults(
    tmp_path: pathlib.Path,
) -> None:
    environment = {
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "blank-optional"),
        "KVB_MAX_BODY_BYTES": "   ",
        "KVB_MAX_DURATION_SECONDS": "\t",
    }

    app = create_app_from_env(environment)

    assert app.state.settings.max_body_bytes == 2 * 1024 * 1024
    assert app.state.settings.max_duration_seconds == 15.0


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("KVB_MAX_BODY_BYTES", "abc"),
        ("KVB_MAX_BODY_BYTES", "1.5"),
        ("KVB_MAX_DURATION_SECONDS", "abc"),
    ],
)
def test_create_app_from_env_rejects_unparseable_numbers(
    tmp_path: pathlib.Path,
    key: str,
    value: str,
) -> None:
    environment = {
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "bad-number"),
        key: value,
    }

    with pytest.raises(RuntimeError, match=key):
        create_app_from_env(environment)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("KVB_MAX_BODY_BYTES", "0"),
        ("KVB_MAX_DURATION_SECONDS", "0"),
        ("KVB_MAX_DURATION_SECONDS", "nan"),
    ],
)
def test_create_app_from_env_invalid_combination_raises_value_error(
    tmp_path: pathlib.Path,
    key: str,
    value: str,
) -> None:
    environment = {
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "invalid-combination"),
        key: value,
    }

    with pytest.raises(ValueError):
        create_app_from_env(environment)


def test_create_app_from_env_rejects_a_storage_path_that_is_a_file(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "not-a-directory"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(RuntimeError):
        create_app_from_env(
            {"KVB_TOKEN": TOKEN, "KVB_STORAGE_DIR": str(path)}
        )


# ---------------------------------------------------------------------------
# Logging and secret hygiene
# ---------------------------------------------------------------------------


def test_success_logs_do_not_contain_token_or_audio(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "PRIVACY-CANARY-7f3a"
    body = _wav(
        frames=100,
        extra_chunks=(_chunk(b"LIST", canary.encode("ascii")),),
    )

    response = client.post(URL, content=body, headers=_headers())

    assert response.status_code == 201
    logs = _log_texts(caplog)
    assert TOKEN not in logs
    assert canary not in logs
    assert body.hex() not in logs
    assert base64.b64encode(body).decode("ascii") not in logs
    assert TOKEN not in response.text
    assert canary not in response.text


def test_auth_failure_logs_do_not_contain_the_presented_token(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    wrong_token = "wrong-token-9c1f"

    response = client.post(
        URL,
        content=_wav(frames=100),
        headers=_headers(auth=f"Bearer {wrong_token}"),
    )

    assert response.status_code == 401
    logs = _log_texts(caplog)
    assert wrong_token not in logs
    assert TOKEN not in logs


# ---------------------------------------------------------------------------
# Health and routing precedence
# ---------------------------------------------------------------------------


def test_healthz_is_unauthenticated_and_leaks_no_config(
    client: TestClient,
    settings: Settings,
) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "api_version": "v1"}
    assert response.headers.get("cache-control") == "no-store"
    assert TOKEN not in response.text
    assert str(settings.storage_dir) not in response.text


def test_wrong_method_on_recordings_is_405_before_auth(client: TestClient) -> None:
    response = client.get(URL)

    _assert_error(response, 405, "method_not_allowed")
    assert "POST" in response.headers["allow"]


def test_unknown_recording_id_is_404(client: TestClient) -> None:
    response = client.get(
        "/v1/recordings/rec_20261110T130700Z_" + "0" * 32
    )

    _assert_error(response, 404, "not_found")


def test_unknown_path_is_404(client: TestClient) -> None:
    response = client.get("/not-a-route")

    _assert_error(response, 404, "not_found")
