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

An optional :class:`voice_bridge_server.trace.TraceStore` records the
``prompt``, ``answer`` and ``sidecars`` stages and the ``llm`` section. Prompt
text, usage, cost and the response text live in the trace file only; no log line
carries them.

``book_search`` turns the single call into the bounded tool loop of
:func:`voice_bridge_server.answer.answer_with_search`. Without it the answer is
the plain call it always was. The loop's tool records go under ``llm.tools`` and
its request count under ``llm.rounds``; queries and passages stay in the trace
and never reach a log line.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from collections.abc import Callable
from typing import Any

from .answer import DEFAULT_ANSWER_MODEL, answer_transcript, answer_with_search
from .trace import TraceStore
from .transcribe import PermanentError, TransientError
from .transcripts import trace_safely, utc_now

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
    context_block: str | None = None,
    on_request: Callable[[dict[str, Any]], None] | None = None,
    trace: str | None = None,
    trace_store: TraceStore | None = None,
    book_search: Callable[..., list[dict[str, Any]]] | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Answer one transcript and write its sidecar. Returns the sidecar data.

    An empty or whitespace-only transcript is not a question, so no request is
    made and nothing is written. An OpenRouter failure is recorded in the
    sidecar with ``retryable`` set, never raised.

    ``context_block`` precedes the question in the user message. ``on_request``
    receives the request body dict before it is sent. ``trace`` is a recording
    id and ``trace_store`` its :class:`voice_bridge_server.trace.TraceStore`;
    both must be set for stages to be recorded.

    ``book_search`` is the search callable of one synced book. When it is set,
    the model may call it a bounded number of times before it answers. When it
    is ``None``, the ask is one plain call with the same messages as before.
    """

    if not isinstance(transcript, str) or not transcript.strip():
        return {"status": "skipped", "text": ""}

    traced = trace is not None and trace_store is not None

    def stage(
        name: str,
        *,
        outcome: str = "ok",
        detail: dict | None = None,
        duration_ms: float | None = None,
    ) -> None:
        """Record one stage on the trace, when there is one."""

        if not traced:
            return
        trace_safely(
            lambda: trace_store.stage(
                trace,
                name,
                outcome=outcome,
                detail=detail,
                duration_ms=duration_ms,
            )
        )

    def set_llm(value: dict) -> None:
        """Merge one ``llm`` section into the trace, when there is one."""

        if not traced:
            return
        trace_safely(lambda: trace_store.set(trace, "llm", value))

    def set_answer(value: dict) -> None:
        """Merge one ``answer`` section into the trace, when there is one."""

        if not traced:
            return
        trace_safely(lambda: trace_store.set(trace, "answer", value))

    def observe(body: dict[str, Any]) -> None:
        """Record the model messages, then hand the body to the caller."""

        messages = body.get("messages")
        model_name = body.get("model")
        system = _message_text(messages, "system")
        user = _message_text(messages, "user")
        effort = body.get("reasoning_effort")
        set_llm(
            {
                "model": model_name if isinstance(model_name, str) else "",
                "prompt": {"system": system, "user": user},
                "reasoning_effort": effort if isinstance(effort, str) else None,
            }
        )
        stage(
            "prompt",
            detail={
                "context": bool(context_block and context_block.strip()),
                "system_chars": len(system),
                "user_chars": len(user),
            },
        )
        if on_request is not None:
            on_request(body)

    def finish(data: dict) -> dict:
        """Write the sidecar as its own stage, then return the sidecar data."""

        started = time.perf_counter()
        try:
            write_answer(wav_path, data)
        except OSError as error:
            stage(
                "sidecars",
                outcome="failed",
                detail={"error": type(error).__name__},
                duration_ms=_elapsed_ms(started),
            )
            raise
        stage("sidecars", duration_ms=_elapsed_ms(started))
        return data

    started = time.perf_counter()
    try:
        if book_search is None:
            answer = answer_transcript(
                transcript,
                api_key,
                model=model,
                transport=transport,
                timeout=timeout,
                context_block=context_block,
                on_request=observe,
                reasoning_effort=reasoning_effort,
            )
        else:
            answer = answer_with_search(
                transcript,
                api_key,
                book_search,
                model=model,
                transport=transport,
                timeout=timeout,
                context_block=context_block,
                on_request=observe,
                reasoning_effort=reasoning_effort,
            )
    except (PermanentError, TransientError) as error:
        stage(
            "answer",
            outcome="failed",
            detail={"error": type(error).__name__},
            duration_ms=_elapsed_ms(started),
        )
        return finish(
            {
                "status": "error",
                "text": "",
                "error": str(error),
                "retryable": isinstance(error, TransientError),
            }
        )

    stage(
        "answer",
        detail={"chars": len(answer.text), "usage": answer.usage is not None},
        duration_ms=_elapsed_ms(started),
    )
    set_answer(
        {"chars_declared": len(answer.text), "chars_sent": len(answer.text)}
    )
    llm: dict = {
        "usage": answer.usage if answer.usage is not None else {},
        "cost_usd": answer.cost,
        "cost_source": "response" if answer.cost is not None else "none",
        "response_text": answer.text,
    }
    if book_search is not None:
        llm["rounds"] = answer.rounds
        llm["tools"] = list(answer.tools)
        if answer.tools_unsupported:
            llm["tools_unsupported"] = True
    set_llm(llm)

    data = {
        "status": "ok",
        "text": answer.text,
        "model": answer.model,
        "usage": answer.usage,
        "updated_at": utc_now(),
    }
    if answer.cost is not None:
        data["cost_usd"] = answer.cost
    return finish(data)


def _message_text(messages: Any, role: str) -> str:
    """Return the content of the first message with ``role``, or ``""``."""

    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != role:
            continue
        content = message.get("content")
        return content if isinstance(content, str) else ""
    return ""


def _elapsed_ms(started: float) -> float:
    """Milliseconds since ``started``, rounded like the trace store does."""

    return round((time.perf_counter() - started) * 1000.0, 3)
