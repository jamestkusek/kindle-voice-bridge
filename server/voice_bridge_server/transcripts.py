"""Transcript sidecars for stored recordings.

One recording produces two files next to the WAV:

    <id>.txt                the transcript, empty when there is no speech
    <id>.transcript.json    status, model, usage, attempts and timestamps

The WAV is only ever opened read-only. Both sidecars are written through a
temporary file and an atomic rename, so a reader never sees a half-written
transcript.

This module owns the file workflow. :mod:`voice_bridge_server.transcribe` owns
the HTTP call and knows nothing about the filesystem. Both the receiver's
background hook and ``tools/transcribe_worker.py`` call
:func:`transcribe_recording`, so there is one implementation of the retry and
sidecar rules.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime, timezone

from .transcribe import (
    DEFAULT_MODEL,
    PermanentError,
    TransientError,
    transcribe_wav,
)

DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_ATTEMPTS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def transcript_paths(wav_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Return the ``(.txt, .json)`` sidecar paths for one recording."""

    return wav_path.with_suffix(".txt"), wav_path.with_suffix(".transcript.json")


def read_sidecar(wav_path: pathlib.Path) -> dict | None:
    """Return the parsed JSON sidecar, or ``None`` when it is missing or bad."""

    _, json_path = transcript_paths(wav_path)
    try:
        with open(json_path, "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_sidecars(wav_path: pathlib.Path, data: dict) -> None:
    """Write both sidecars atomically. Never touches the WAV."""

    text_path, json_path = transcript_paths(wav_path)
    for path, body in (
        (text_path, data.get("text") or ""),
        (json_path, json.dumps(data, indent=2, sort_keys=True) + "\n"),
    ):
        temporary = path.with_name(path.name + ".tmp")
        with open(temporary, "w", encoding="utf-8", newline="\n") as target:
            target.write(body)
        os.replace(temporary, path)


def is_pending(wav_path: pathlib.Path, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    """Return True when the recording still needs a transcription attempt."""

    sidecar = read_sidecar(wav_path)
    if sidecar is None:
        return True
    if sidecar.get("status") == "ok":
        return False
    attempts = sidecar.get("attempts")
    if not isinstance(attempts, int):
        return True
    return attempts < max_attempts


def transcribe_recording(
    wav_path: pathlib.Path,
    api_key: str,
    model: str = DEFAULT_MODEL,
    transport=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Transcribe one file and write its sidecars. Returns the sidecar data.

    A failure is recorded in the sidecar rather than raised, so a caller that
    must not fail (the receiver's upload path) can call this directly. The
    ``retryable`` field says whether the failure was transient.
    """

    previous = read_sidecar(wav_path) or {}
    attempts = previous.get("attempts")
    attempts = attempts + 1 if isinstance(attempts, int) else 1
    started = time.monotonic()

    try:
        wav_bytes = wav_path.read_bytes()
    except OSError as error:
        data = {
            "status": "error",
            "text": "",
            "attempts": attempts,
            "error": f"The recording could not be read: {error.strerror or error}.",
            "retryable": False,
            "updated_at": utc_now(),
        }
        write_sidecars(wav_path, data)
        return data

    try:
        transcript = transcribe_wav(
            wav_bytes, api_key, model=model, transport=transport, timeout=timeout
        )
    except (PermanentError, TransientError) as error:
        data = {
            "status": "error",
            "text": "",
            "attempts": attempts,
            "model": model,
            "error": str(error),
            "retryable": isinstance(error, TransientError),
            "updated_at": utc_now(),
        }
        write_sidecars(wav_path, data)
        return data

    data = {
        "status": "ok",
        "text": transcript.text,
        "attempts": attempts,
        "model": transcript.model,
        "usage": transcript.usage,
        "seconds": round(time.monotonic() - started, 3),
        "updated_at": utc_now(),
    }
    write_sidecars(wav_path, data)
    return data
