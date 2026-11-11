"""Tests for the development review page and its JSON/playback/delete routes.

These routes are outside the upload contract in ``docs/protocol.md``. The tests
use FastAPI's in-process ``TestClient`` and a pytest ``tmp_path`` directory; no
network, device or real recording is used.
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from voice_bridge_server.app import create_app
from voice_bridge_server.config import Settings
from voice_bridge_server.storage import new_recording_id

try:  # pragma: no cover - branch depends on the pytest import mode
    from . import _wav_builders
except ImportError:  # pragma: no cover
    import _wav_builders

TOKEN = "test-token-0123456789abcdef"
SHA_PREFIX_CHARS = 12


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


def _wav(*, frames: int = 100, rate: int = 8000, bits: int = 16) -> bytes:
    return _wav_builders.build_wav(frames=frames, rate=rate, bits=bits)


def _store(
    storage_dir: pathlib.Path,
    body: bytes,
    *,
    recording_id: str | None = None,
    mtime: float | None = None,
) -> tuple[str, pathlib.Path]:
    recording_id = recording_id or new_recording_id()
    path = storage_dir / f"{recording_id}.wav"
    path.write_bytes(body)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return recording_id, path


def test_list_reflects_stored_files_newest_first(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    older_body = _wav(frames=100)
    newer_body = _wav(frames=1600, rate=16000)
    older_id, _ = _store(storage_dir, older_body, mtime=1_700_000_000)
    newer_id, _ = _store(storage_dir, newer_body, mtime=1_700_000_100)

    response = client.get("/api/recordings")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["count"] == 2
    assert [item["id"] for item in payload["recordings"]] == [
        newer_id,
        older_id,
    ]

    newest = payload["recordings"][0]
    assert newest["size_bytes"] == len(newer_body)
    assert newest["sha256_prefix"] == hashlib.sha256(newer_body).hexdigest()[
        :SHA_PREFIX_CHARS
    ]
    assert newest["duration_seconds"] == 0.1
    assert newest["sample_rate_hz"] == 16000
    assert newest["channels"] == 1
    assert newest["bits_per_sample"] == 16
    assert newest["parse_error"] is None
    assert newest["received_at"].startswith("2023-11-14T")


def test_uploaded_recording_appears_in_listing(client: TestClient) -> None:
    body = _wav(frames=100)

    upload = client.post(
        "/v1/recordings",
        content=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "audio/wav",
        },
    )

    assert upload.status_code == 201
    recording_id = upload.json()["id"]
    payload = client.get("/api/recordings").json()
    assert [item["id"] for item in payload["recordings"]] == [recording_id]


def test_playback_returns_exact_bytes(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    body = _wav(frames=321)
    recording_id, _ = _store(storage_dir, body)

    response = client.get(f"/recordings/{recording_id}.wav")

    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-disposition"].startswith("inline")
    assert recording_id in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_playback_download_uses_attachment(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    recording_id, _ = _store(storage_dir, _wav())

    response = client.get(f"/recordings/{recording_id}.wav?download=1")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


def test_unknown_recording_id_is_rejected(client: TestClient) -> None:
    missing = new_recording_id()

    response = client.get(f"/recordings/{missing}.wav")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-recording",
        "rec_20260101T000000Z_short",
        "rec_20260101T000000Z_" + "A" * 32,
        "rec_20260101T000000Z_" + "0" * 32 + "x",
        "..",
    ],
)
def test_malformed_recording_id_is_rejected(
    client: TestClient, bad_id: str
) -> None:
    response = client.get(f"/recordings/{bad_id}.wav")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_recording_id"


def test_path_traversal_attempt_does_not_read_outside_storage(
    client: TestClient, tmp_path: pathlib.Path
) -> None:
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"outside the storage directory")

    response = client.get("/recordings/..%2Fsecret.wav")

    assert response.status_code in {400, 404}
    assert b"outside the storage directory" not in response.content


def test_delete_removes_file_and_updates_listing(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    keep_id, keep_path = _store(storage_dir, _wav(frames=100))
    drop_id, drop_path = _store(storage_dir, _wav(frames=200))

    response = client.delete(f"/api/recordings/{drop_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] == drop_id
    assert payload["count"] == 1
    assert [item["id"] for item in payload["recordings"]] == [keep_id]
    assert not drop_path.exists()
    assert keep_path.exists()
    assert client.get(f"/recordings/{drop_id}.wav").status_code == 404
    assert [
        item["id"]
        for item in client.get("/api/recordings").json()["recordings"]
    ] == [keep_id]


def test_delete_unknown_or_malformed_id_is_rejected(
    client: TestClient,
) -> None:
    missing = new_recording_id()

    unknown = client.delete(f"/api/recordings/{missing}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"

    malformed = client.delete("/api/recordings/not-an-id")
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_recording_id"


def test_token_never_appears_in_review_responses(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    recording_id, _ = _store(storage_dir, _wav())

    responses = [
        client.get("/"),
        client.get("/api/recordings"),
        client.get(f"/recordings/{recording_id}.wav"),
        client.delete(f"/api/recordings/{recording_id}"),
        client.get("/recordings/not-an-id.wav"),
    ]

    for response in responses:
        assert TOKEN.encode() not in response.content


def test_review_page_is_self_contained(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    text = response.text
    assert "<script src" not in text
    assert "cdn" not in text.lower()
    assert "http://" not in text
    assert "https://" not in text
    assert "/api/recordings" in text


def test_list_ignores_files_that_are_not_server_recordings(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    _store(storage_dir, _wav())
    (storage_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    (storage_dir / ".incoming-abc.part").write_bytes(b"partial")
    (storage_dir / "not-a-recording.wav").write_bytes(_wav())

    payload = client.get("/api/recordings").json()

    assert payload["count"] == 1


def test_unparseable_stored_file_is_listed_with_parse_error(
    client: TestClient, storage_dir: pathlib.Path
) -> None:
    body = b"RIFF....not really a wav"
    recording_id, _ = _store(storage_dir, body)

    payload = client.get("/api/recordings").json()

    item = payload["recordings"][0]
    assert item["id"] == recording_id
    assert item["parse_error"] == "invalid_wav"
    assert item["duration_seconds"] is None
    assert item["size_bytes"] == len(body)
    assert client.get(f"/recordings/{recording_id}.wav").content == body
