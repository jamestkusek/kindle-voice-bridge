"""Answer sidecars for stored recordings.

One answered recording produces one file next to the WAV:

    <id>.answer.json    status, text, model, usage and the error, if any

The WAV is only ever opened read-only, and it is never needed for the answer
itself: the transcript text is passed in. The sidecar is written through a
temporary file and an atomic rename, so a reader never sees a half-written
answer.

This module owns the file workflow. :mod:`voice_bridge_server.answer` owns the
HTTP call and knows nothing about the filesystem. A failure is recorded in the
sidecar rather than raised, so the receiver's background hook can call
:func:`answer_recording` without a try block.
"""

from __future__ import annotations

import json
import os
import pathlib

from .answer import DEFAULT_ANSWER_MODEL, answer_transcript
from .transcribe import PermanentError, TransientError
from .transcripts import utc_now

DEFAULT_TIMEOUT_SECONDS = 60.0


def answer_path(wav_path: pathlib.Path) -> pathlib.Path:
    """Return the ``.answer.json`` sidecar path for one recording."""

    return wav_path.with_suffix(".answer.json")


def read_answer(wav_path: pathlib.Path) -> dict | None:
    """Return the parsed answer sidecar, or ``None`` when it is missing or bad."""

    try:
        with open(answer_path(wav_path), "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_answer(wav_path: pathlib.Path, data: dict) -> None:
    """Write the answer sidecar atomically. Never touches the WAV."""

    path = answer_path(wav_path)
    body = json.dumps(data, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as target:
        target.write(body)
    os.replace(temporary, path)


def answer_recording(
    wav_path: pathlib.Path,
    transcript: str,
    api_key: str,
    model: str = DEFAULT_ANSWER_MODEL,
    transport=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Answer one transcript and write its sidecar. Returns the sidecar data.

    An empty or whitespace-only transcript is not a question, so no request is
    made and nothing is written. An OpenRouter failure is recorded in the
    sidecar with ``retryable`` set, never raised.
    """

    if not isinstance(transcript, str) or not transcript.strip():
        return {"status": "skipped", "text": ""}

    try:
        answer = answer_transcript(
            transcript, api_key, model=model, transport=transport, timeout=timeout
        )
    except (PermanentError, TransientError) as error:
        data = {
            "status": "error",
            "text": "",
            "error": str(error),
            "retryable": isinstance(error, TransientError),
        }
        write_answer(wav_path, data)
        return data

    data = {
        "status": "ok",
        "text": answer.text,
        "model": answer.model,
        "usage": answer.usage,
        "updated_at": utc_now(),
    }
    write_answer(wav_path, data)
    return data
