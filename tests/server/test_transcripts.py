"""Offline tests for the transcription sidecar workflow and its trace stage.

No test touches the network. ``transcribe_recording`` takes an injected
transport, so every outcome is exercised with a fake.
"""

import json
import logging
import pathlib

import pytest

from voice_bridge_server.trace import TraceStore
from voice_bridge_server.transcribe import DEFAULT_MODEL
from voice_bridge_server.transcripts import (
    read_sidecar,
    trace_safely,
    transcript_paths,
    transcribe_recording,
)

RECORDING_ID = "rec_20260101T000000Z_" + "a" * 32
WAV_BYTES = b"RIFF" + b"\x00" * 40


def ok_body(text="hello there"):
    return json.dumps({
        "model": DEFAULT_MODEL,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }).encode("utf-8")


class FakeTransport:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else ok_body()
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": dict(headers)})
        return self.status, self.body


class FailingTraceStore:
    """A trace store whose every write fails, like a full disk."""

    def stage(self, *args, **kwargs):
        raise OSError("simulated disk full")

    def read(self, recording_id):
        return None


def make_recording(directory: pathlib.Path):
    path = directory / f"{RECORDING_ID}.wav"
    path.write_bytes(WAV_BYTES)
    return path


def stages(store: TraceStore, recording_id: str = RECORDING_ID) -> list[dict]:
    return store.read(recording_id)["stages"]


def test_a_successful_attempt_records_one_transcribe_stage(tmp_path):
    store = TraceStore(tmp_path)
    store.start(RECORDING_ID, origin="kindle")
    wav = make_recording(tmp_path)

    data = transcribe_recording(
        wav,
        "secret-key",
        transport=FakeTransport(),
        trace=RECORDING_ID,
        trace_store=store,
    )

    assert data["status"] == "ok"
    assert data["text"] == "hello there"
    recorded = stages(store)
    assert [stage["name"] for stage in recorded] == ["transcribe"]
    assert recorded[0]["outcome"] == "ok"
    assert recorded[0]["detail"]["attempts"] == 1
    assert recorded[0]["detail"]["chars"] == len("hello there")
    assert isinstance(recorded[0]["dur_ms"], float)


def test_a_permanent_failure_records_a_failed_stage(tmp_path):
    store = TraceStore(tmp_path)
    store.start(RECORDING_ID, origin="kindle")
    wav = make_recording(tmp_path)

    data = transcribe_recording(
        wav,
        "secret-key",
        transport=FakeTransport(status=401),
        trace=RECORDING_ID,
        trace_store=store,
    )

    assert data["status"] == "error"
    assert data["retryable"] is False
    recorded = stages(store)
    assert recorded[0]["outcome"] == "failed"
    assert recorded[0]["detail"] == {"attempts": 1, "retryable": False}


def test_tracing_does_not_change_the_sidecars(tmp_path):
    traced = tmp_path / "traced"
    plain = tmp_path / "plain"
    for directory in (traced, plain):
        directory.mkdir()

    store = TraceStore(traced)
    store.start(RECORDING_ID, origin="kindle")
    transcribe_recording(
        make_recording(traced),
        "secret-key",
        transport=FakeTransport(),
        trace=RECORDING_ID,
        trace_store=store,
    )

    plain_path = make_recording(plain)
    transcribe_recording(
        plain_path, "secret-key", transport=FakeTransport()
    )

    traced_sidecar = read_sidecar(traced / f"{RECORDING_ID}.wav")
    plain_sidecar = read_sidecar(plain_path)
    assert traced_sidecar.keys() == plain_sidecar.keys()
    for key in plain_sidecar:
        if key in ("seconds", "updated_at"):
            continue
        assert traced_sidecar[key] == plain_sidecar[key]
    assert not (plain / f"{RECORDING_ID}.trace.json").exists()


def test_without_a_trace_no_trace_file_is_written(tmp_path):
    wav = make_recording(tmp_path)

    transcribe_recording(wav, "secret-key", transport=FakeTransport())

    text_path, json_path = transcript_paths(wav)
    assert text_path.exists()
    assert json_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [wav.name, text_path.name, json_path.name]
    )


def test_trace_safely_logs_the_type_and_does_not_raise(caplog):
    def failing_write():
        raise PermissionError("book text must not be logged")

    with caplog.at_level(logging.WARNING, logger="voice_bridge_server.transcripts"):
        trace_safely(failing_write)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "PermissionError" in record.getMessage()
    assert "book text must not be logged" not in record.getMessage()
    assert record.args == ("PermissionError",)


def test_trace_safely_ignores_a_successful_write(caplog):
    calls = []

    with caplog.at_level(logging.WARNING, logger="voice_bridge_server.transcripts"):
        trace_safely(lambda: calls.append(1))

    assert calls == [1]
    assert caplog.records == []


def test_a_trace_write_failure_does_not_lose_the_sidecar(tmp_path):
    wav = make_recording(tmp_path)

    data = transcribe_recording(
        wav,
        "secret-key",
        transport=FakeTransport(),
        trace=RECORDING_ID,
        trace_store=FailingTraceStore(),
    )

    assert data["status"] == "ok"
    assert read_sidecar(wav)["text"] == "hello there"


@pytest.mark.parametrize("status,retryable", [(401, False), (503, True)])
def test_the_retry_fields_are_unchanged(tmp_path, status, retryable):
    wav = make_recording(tmp_path)

    data = transcribe_recording(
        wav, "secret-key", transport=FakeTransport(status=status)
    )

    assert data["retryable"] is retryable
    assert data["attempts"] == 1
