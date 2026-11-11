"""Offline tests for the answer path, its sidecar and the answer endpoint.

No test touches the network. ``answer_transcript`` takes an injected transport,
so every response shape is exercised with a fake.
"""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from voice_bridge_server import answer as answer_module
from voice_bridge_server.answer import (
    ANSWER_PROMPT,
    DEFAULT_ANSWER_MODEL,
    PermanentError,
    TransientError,
    answer_transcript,
    build_request_body,
    parse_response,
)
from voice_bridge_server.answers import (
    answer_path,
    answer_recording,
    read_answer,
)
from voice_bridge_server.app import create_app, create_app_from_env
from voice_bridge_server.config import Settings

try:  # pragma: no cover - branch depends on the pytest import mode
    from . import _wav_builders
except ImportError:  # pragma: no cover
    import _wav_builders

TOKEN = "test-token-0123456789abcdef"
WAV_BODY = _wav_builders.build_wav(frames=100)
RECORDING_ID = "rec_20260101T000000Z_" + "a" * 32


def ok_body(text="Paris.", model=DEFAULT_ANSWER_MODEL):
    return json.dumps({
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
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


def make_recording(directory: pathlib.Path, name=RECORDING_ID):
    path = directory / f"{name}.wav"
    path.write_bytes(WAV_BODY)
    return path


# Request shape -------------------------------------------------------------


def test_request_body_is_plain_text_with_no_audio():
    body = build_request_body("What is the capital of France?")

    assert body["model"] == DEFAULT_ANSWER_MODEL
    assert body["temperature"] == 0
    messages = body["messages"]
    assert messages[0] == {"role": "system", "content": ANSWER_PROMPT}
    assert messages[1] == {
        "role": "user",
        "content": "What is the capital of France?",
    }
    assert "input_audio" not in json.dumps(body)


def test_request_body_rejects_an_empty_transcript():
    with pytest.raises(PermanentError):
        build_request_body("   ")


# Transport -----------------------------------------------------------------


def test_answer_transcript_sends_the_key_only_in_the_header():
    transport = FakeTransport()

    result = answer_transcript("Who wrote Dune?", "secret-key", transport=transport)

    assert result.text == "Paris."
    assert result.model == DEFAULT_ANSWER_MODEL
    call = transport.calls[0]
    assert call["url"] == answer_module.ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert b"secret-key" not in call["payload"]
    assert call["timeout"] == 60.0


def test_parse_response_treats_an_auth_failure_as_permanent():
    with pytest.raises(PermanentError):
        parse_response(401, b"{}", DEFAULT_ANSWER_MODEL)


def test_parse_response_treats_a_rate_limit_as_transient():
    with pytest.raises(TransientError):
        parse_response(429, b"{}", DEFAULT_ANSWER_MODEL)


# Sidecar -------------------------------------------------------------------


def test_answer_recording_writes_the_sidecar(tmp_path):
    wav = make_recording(tmp_path)

    data = answer_recording(
        wav, "Who wrote Dune?", "secret-key", transport=FakeTransport()
    )

    assert data["status"] == "ok"
    assert data["text"] == "Paris."
    assert data["model"] == DEFAULT_ANSWER_MODEL
    assert data["usage"]["completion_tokens"] == 3
    assert data["updated_at"]
    assert answer_path(wav).name == f"{RECORDING_ID}.answer.json"
    assert read_answer(wav) == data
    assert wav.read_bytes() == WAV_BODY


def test_answer_recording_records_a_permanent_failure(tmp_path):
    wav = make_recording(tmp_path)

    data = answer_recording(
        wav, "Who wrote Dune?", "secret-key", transport=FakeTransport(status=401)
    )

    assert data["status"] == "error"
    assert data["text"] == ""
    assert data["retryable"] is False
    assert "secret-key" not in data["error"]
    assert read_answer(wav)["status"] == "error"


def test_answer_recording_records_a_transient_failure(tmp_path):
    wav = make_recording(tmp_path)

    data = answer_recording(
        wav, "Who wrote Dune?", "secret-key", transport=FakeTransport(status=503)
    )

    assert data["status"] == "error"
    assert data["retryable"] is True


def test_an_empty_transcript_is_skipped_without_a_call(tmp_path):
    wav = make_recording(tmp_path)
    transport = FakeTransport()

    data = answer_recording(wav, "   ", "secret-key", transport=transport)

    assert transport.calls == []
    assert data["status"] == "skipped"
    assert not answer_path(wav).exists()


# Endpoint ------------------------------------------------------------------


def make_client(tmp_path, *, transcribe=None, answer=None):
    settings = Settings(token=TOKEN, storage_dir=tmp_path / "recordings")
    app = create_app(settings, transcribe=transcribe, answer=answer)
    return app, TestClient(app)


def upload(client):
    return client.post(
        "/v1/recordings",
        content=WAV_BODY,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "audio/wav",
        },
    )


def get_answer(client, recording_id=RECORDING_ID, auth=f"Bearer {TOKEN}"):
    headers = {} if auth is None else {"Authorization": auth}
    return client.get(f"/v1/recordings/{recording_id}/answer", headers=headers)


def test_endpoint_reports_a_ready_answer(tmp_path):
    _, client = make_client(tmp_path)
    make_recording(tmp_path / "recordings")
    answer_recording(
        tmp_path / "recordings" / f"{RECORDING_ID}.wav",
        "Who wrote Dune?",
        "secret-key",
        transport=FakeTransport(),
    )

    response = get_answer(client)

    assert response.status_code == 200
    assert response.json() == {
        "id": RECORDING_ID,
        "status": "ready",
        "text": "Paris.",
        "model": DEFAULT_ANSWER_MODEL,
    }


def test_endpoint_reports_a_pending_answer(tmp_path):
    _, client = make_client(tmp_path)
    make_recording(tmp_path / "recordings")

    response = get_answer(client)

    assert response.status_code == 200
    assert response.json() == {"id": RECORDING_ID, "status": "pending"}


def test_endpoint_reports_a_failed_answer(tmp_path):
    _, client = make_client(tmp_path)
    wav = make_recording(tmp_path / "recordings")
    answer_recording(
        wav, "Who wrote Dune?", "secret-key", transport=FakeTransport(status=401)
    )

    response = get_answer(client)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == RECORDING_ID
    assert body["status"] == "failed"
    assert body["error"]


@pytest.mark.parametrize(
    "recording_id", ["rec_missing", RECORDING_ID.replace("a", "b")]
)
def test_endpoint_returns_404_for_an_unknown_recording(tmp_path, recording_id):
    _, client = make_client(tmp_path)

    response = get_answer(client, recording_id)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Basic abc"])
def test_endpoint_rejects_a_bad_token(tmp_path, auth):
    _, client = make_client(tmp_path)

    response = get_answer(client, auth=auth)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


# Upload wiring -------------------------------------------------------------


def test_upload_answers_a_successful_transcript(tmp_path):
    seen = []

    def transcribe(path):
        return {"status": "ok", "text": "Who wrote Dune?"}

    def answer(path, transcript):
        seen.append((path, transcript))

    _, client = make_client(tmp_path, transcribe=transcribe, answer=answer)

    with client:
        response = upload(client)

    assert response.status_code == 201
    assert len(seen) == 1
    assert seen[0][0].name == response.json()["id"] + ".wav"
    assert seen[0][1] == "Who wrote Dune?"


def test_upload_skips_the_answer_for_an_empty_transcript(tmp_path):
    seen = []
    _, client = make_client(
        tmp_path,
        transcribe=lambda path: {"status": "ok", "text": ""},
        answer=lambda path, transcript: seen.append(transcript),
    )

    with client:
        upload(client)

    assert seen == []


def test_upload_response_is_unchanged_when_the_answer_raises(tmp_path):
    def explode(path, transcript):
        raise RuntimeError("boom")

    _, client = make_client(
        tmp_path,
        transcribe=lambda path: {"status": "ok", "text": "Who wrote Dune?"},
        answer=explode,
    )

    with client:
        response = upload(client)

    assert response.status_code == 201
    assert response.json()["status"] == "stored"


def test_answering_is_off_without_a_key(tmp_path):
    app = create_app_from_env({
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
    })

    assert app.state.answer is None


def test_answering_is_on_with_a_key_and_the_model_can_be_overridden(tmp_path):
    app = create_app_from_env({
        "KVB_TOKEN": TOKEN,
        "KVB_STORAGE_DIR": str(tmp_path / "recordings"),
        "OPENROUTER_API_KEY": "sk-or-v1-test",
        "KVB_ANSWER_MODEL": "openai/gpt-4o-mini",
    })

    assert callable(app.state.answer)
