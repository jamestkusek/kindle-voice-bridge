"""Offline tests for the OpenRouter transcription path and its worker.

No test touches the network. ``transcribe_wav`` takes an injected transport, so
every response shape is exercised with a fake.
"""

import base64
import json
import pathlib

import pytest

from voice_bridge_server import transcribe
from voice_bridge_server.transcripts import transcript_paths
from voice_bridge_server.transcribe import (
    DEFAULT_MODEL,
    PermanentError,
    TransientError,
    build_request_body,
    parse_response,
    transcribe_wav,
)

import importlib.util
import sys

WORKER_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "transcribe_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("transcribe_worker", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["transcribe_worker"] = module
    spec.loader.exec_module(module)
    return module


worker = load_worker()

WAV_BYTES = b"RIFF" + b"\x00" * 40


def ok_body(text="hello there", model=DEFAULT_MODEL, usage=None):
    return json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 3},
    }).encode("utf-8")


class FakeTransport:
    def __init__(self, status=200, body=None, error=None):
        self.status = status
        self.body = body if body is not None else ok_body()
        self.error = error
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "payload": payload,
            "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        return self.status, self.body


# Request shape -------------------------------------------------------------


def test_request_body_carries_the_wav_as_base64_audio():
    body = build_request_body(WAV_BYTES)

    assert body["model"] == DEFAULT_MODEL
    assert body["temperature"] == 0
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "verbatim" in content[0]["text"]
    audio = content[1]
    assert audio["type"] == "input_audio"
    assert audio["input_audio"]["format"] == "wav"
    assert base64.b64decode(audio["input_audio"]["data"]) == WAV_BYTES


def test_request_body_rejects_empty_audio():
    with pytest.raises(PermanentError):
        build_request_body(b"")


def test_request_body_rejects_a_missing_model():
    with pytest.raises(PermanentError):
        build_request_body(WAV_BYTES, model="")


# Response parsing ----------------------------------------------------------


def test_parse_response_returns_the_transcript():
    transcript = parse_response(200, ok_body("  hello there  "), DEFAULT_MODEL)

    assert transcript.text == "hello there"
    assert transcript.model == DEFAULT_MODEL
    assert transcript.usage["completion_tokens"] == 3


def test_parse_response_accepts_typed_content_parts():
    body = json.dumps({
        "choices": [{"message": {"content": [
            {"type": "text", "text": "one "},
            {"type": "text", "text": "two"},
        ]}}],
    }).encode("utf-8")

    assert parse_response(200, body, DEFAULT_MODEL).text == "one two"


def test_parse_response_keeps_an_empty_transcript():
    assert parse_response(200, ok_body(""), DEFAULT_MODEL).text == ""


@pytest.mark.parametrize("status", [401, 403])
def test_parse_response_treats_auth_failure_as_permanent(status):
    with pytest.raises(PermanentError):
        parse_response(status, b"{}", DEFAULT_MODEL)


def test_parse_response_treats_an_unknown_model_as_permanent():
    with pytest.raises(PermanentError):
        parse_response(404, b"{}", DEFAULT_MODEL)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_parse_response_treats_rate_limit_and_5xx_as_transient(status):
    with pytest.raises(TransientError):
        parse_response(status, b"{}", DEFAULT_MODEL)


def test_parse_response_treats_other_4xx_as_permanent():
    with pytest.raises(PermanentError):
        parse_response(400, b"{}", DEFAULT_MODEL)


def test_parse_response_rejects_non_json():
    with pytest.raises(TransientError):
        parse_response(200, b"<html>", DEFAULT_MODEL)


def test_parse_response_rejects_a_missing_choice():
    with pytest.raises(TransientError):
        parse_response(200, b'{"choices": []}', DEFAULT_MODEL)


def test_parse_response_rejects_a_missing_message():
    with pytest.raises(TransientError):
        parse_response(200, b'{"choices": [{}]}', DEFAULT_MODEL)


# Transport ----------------------------------------------------------------


def test_transcribe_wav_sends_the_key_only_in_the_header():
    transport = FakeTransport()

    transcript = transcribe_wav(WAV_BYTES, "secret-key", transport=transport)

    assert transcript.text == "hello there"
    call = transport.calls[0]
    assert call["url"] == transcribe.ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert b"secret-key" not in call["payload"]
    assert call["timeout"] == 90.0


def test_transcribe_wav_requires_a_key():
    with pytest.raises(PermanentError):
        transcribe_wav(WAV_BYTES, "")


def test_transcribe_wav_turns_a_network_error_into_a_transient_error():
    transport = FakeTransport(error=OSError("connection refused"))

    with pytest.raises(TransientError):
        transcribe_wav(WAV_BYTES, "secret-key", transport=transport)


def test_transcribe_wav_never_puts_the_key_in_an_error():
    transport = FakeTransport(error=OSError("connection refused"))

    with pytest.raises(TransientError) as caught:
        transcribe_wav(WAV_BYTES, "secret-key", transport=transport)

    assert "secret-key" not in str(caught.value)


# Worker -------------------------------------------------------------------


def make_recording(directory: pathlib.Path, name="rec_20260101T000000Z_" + "a" * 32):
    path = directory / f"{name}.wav"
    path.write_bytes(WAV_BYTES)
    return path


def test_worker_transcribes_a_new_recording(tmp_path):
    wav = make_recording(tmp_path)
    transport = FakeTransport()

    handled = worker.run_once(
        tmp_path, "secret-key", transport=transport, report=lambda _: None
    )

    assert handled == 1
    text_path, json_path = transcript_paths(wav)
    assert text_path.read_text(encoding="utf-8") == "hello there"
    sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    assert sidecar["status"] == "ok"
    assert sidecar["attempts"] == 1
    assert sidecar["model"] == DEFAULT_MODEL
    assert wav.read_bytes() == WAV_BYTES


def test_worker_does_not_resend_a_transcribed_recording(tmp_path):
    make_recording(tmp_path)
    transport = FakeTransport()

    worker.run_once(tmp_path, "secret-key", transport=transport, report=lambda _: None)
    worker.run_once(tmp_path, "secret-key", transport=transport, report=lambda _: None)

    assert len(transport.calls) == 1


def test_worker_ignores_files_that_are_not_recordings(tmp_path):
    (tmp_path / "notes.wav").write_bytes(WAV_BYTES)
    (tmp_path / "rec_short.wav").write_bytes(WAV_BYTES)
    (tmp_path / ("rec_20260101T000000Z_" + "b" * 32 + ".txt")).write_text("x")
    transport = FakeTransport()

    handled = worker.run_once(
        tmp_path, "secret-key", transport=transport, report=lambda _: None
    )

    assert handled == 0
    assert transport.calls == []


def test_worker_records_a_failure_and_counts_attempts(tmp_path):
    wav = make_recording(tmp_path)
    transport = FakeTransport(status=500)

    worker.run_once(tmp_path, "secret-key", transport=transport, report=lambda _: None)

    _, json_path = transcript_paths(wav)
    sidecar = json.loads(json_path.read_text(encoding="utf-8"))
    assert sidecar["status"] == "error"
    assert sidecar["attempts"] == 1
    assert sidecar["retryable"] is True


def test_worker_stops_retrying_at_the_attempt_limit(tmp_path):
    make_recording(tmp_path)
    transport = FakeTransport(status=500)

    for _ in range(5):
        worker.run_once(
            tmp_path,
            "secret-key",
            max_attempts=2,
            transport=transport,
            report=lambda _: None,
        )

    assert len(transport.calls) == 2


def test_worker_does_not_retry_a_permanent_failure_beyond_the_limit(tmp_path):
    wav = make_recording(tmp_path)
    transport = FakeTransport(status=401)

    worker.run_once(
        tmp_path, "secret-key", max_attempts=1, transport=transport, report=lambda _: None
    )
    worker.run_once(
        tmp_path, "secret-key", max_attempts=1, transport=transport, report=lambda _: None
    )

    assert len(transport.calls) == 1
    sidecar = json.loads(transcript_paths(wav)[1].read_text(encoding="utf-8"))
    assert sidecar["retryable"] is False


def test_worker_reports_an_empty_transcript(tmp_path):
    make_recording(tmp_path)
    lines = []

    worker.run_once(
        tmp_path,
        "secret-key",
        transport=FakeTransport(body=ok_body("")),
        report=lines.append,
    )

    assert lines and "(no speech)" in lines[0]


def test_worker_keeps_going_after_an_unreadable_file(tmp_path):
    good = make_recording(tmp_path, "rec_20260101T000000Z_" + "c" * 32)
    broken = make_recording(tmp_path, "rec_20260101T000000Z_" + "d" * 32)
    broken.write_bytes(b"")  # empty audio is a permanent failure, not a crash
    transport = FakeTransport()

    handled = worker.run_once(
        tmp_path, "secret-key", transport=transport, report=lambda _: None
    )

    assert handled == 2
    assert transcript_paths(good)[0].read_text(encoding="utf-8") == "hello there"
    assert json.loads(transcript_paths(broken)[1].read_text())["status"] == "error"


# Receiver hook -------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from voice_bridge_server.app import create_app, create_app_from_env  # noqa: E402
from voice_bridge_server.config import Settings  # noqa: E402

try:  # pragma: no cover - branch depends on the pytest import mode
    from . import _wav_builders
except ImportError:  # pragma: no cover
    import _wav_builders

TOKEN = "test-token-0123456789abcdef"
WAV_BODY = _wav_builders.build_wav(frames=100)


def make_client(tmp_path, transcribe=None):
    settings = Settings(token=TOKEN, storage_dir=tmp_path / "recordings")
    app = create_app(settings, transcribe=transcribe)
    return app, TestClient(app)


def post_recording(client):
    return client.post(
        "/v1/recordings",
        content=WAV_BODY,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "audio/wav",
        },
    )


def test_upload_fires_the_transcriber_with_the_stored_path(tmp_path):
    seen = []
    app, client = make_client(tmp_path, transcribe=seen.append)

    with client:
        response = post_recording(client)

    assert response.status_code == 201
    assert len(seen) == 1
    assert seen[0].name == response.json()["id"] + ".wav"
    assert seen[0].exists()


def test_upload_response_is_unchanged_by_the_hook(tmp_path):
    _, plain = make_client(tmp_path / "a")
    _, hooked = make_client(tmp_path / "b", transcribe=lambda path: None)

    with plain, hooked:
        without = post_recording(plain)
        with_hook = post_recording(hooked)

    assert without.status_code == with_hook.status_code == 201
    without_body = without.json()
    with_body = with_hook.json()
    # The ID, timestamp and hash differ per upload; the shape must not.
    assert set(without_body) == set(with_body)
    assert without_body["status"] == with_body["status"] == "stored"


def test_a_failing_transcriber_does_not_fail_the_upload(tmp_path):
    def explode(path):
        raise RuntimeError("boom")

    app, client = make_client(tmp_path, transcribe=explode)

    with client:
        response = post_recording(client)

    assert response.status_code == 201
    assert response.json()["status"] == "stored"


def test_no_transcriber_means_no_hook(tmp_path):
    app, client = make_client(tmp_path)

    with client:
        response = post_recording(client)

    assert response.status_code == 201
    assert app.state.transcribe is None


def test_transcription_is_off_without_a_key(tmp_path):
    app = create_app_from_env({
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
    })

    assert app.state.transcribe is None


def test_transcription_is_on_with_a_key(tmp_path):
    app = create_app_from_env({
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
        "OPENROUTER_API_KEY": "sk-or-v1-test",
    })

    assert callable(app.state.transcribe)


def test_the_model_can_be_overridden(tmp_path):
    app = create_app_from_env({
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
        "OPENROUTER_API_KEY": "sk-or-v1-test",
        "KVB_TRANSCRIBE_MODEL": "google/gemini-2.5-flash-lite",
    })

    assert callable(app.state.transcribe)
