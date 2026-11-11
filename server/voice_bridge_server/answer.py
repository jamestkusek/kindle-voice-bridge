"""Answers to transcribed questions through OpenRouter.

This is the step after :mod:`voice_bridge_server.transcribe`. A transcript is
sent as ordinary chat text to a cheap model, and the reply is the answer that
the Kindle fetches later.

The module mirrors ``transcribe.py`` on purpose. It is pure apart from the
injected transport: :func:`answer_transcript` takes a callable that performs one
HTTP POST and returns ``(status, body)``, so tests never touch the network. The
error vocabulary is shared with transcription -- :class:`transcribe.TransientError`
and :class:`transcribe.PermanentError` -- so a caller has one set of rules to
follow.

The API key travels only in the ``Authorization`` header. It is never put in the
request body, a log line, or an error message.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .transcribe import ENDPOINT, PermanentError, TransientError, Transport

#: Cheap and fast. The answer is a couple of sentences, so a large model buys
#: nothing here.
DEFAULT_ANSWER_MODEL = "mistralai/mistral-small-24b-instruct-2501"

#: The transcript comes from an 8 kHz headset mic, so it may be a question, a
#: fragment, or noise. The prompt keeps the reply short either way and forbids
#: the preamble chat models add by default.
ANSWER_PROMPT = (
    "Answer the question in the user message directly, in two sentences or "
    "fewer. If the message is not a question, reply with one short sentence. "
    "Output only the answer: no preamble, no restating the question, no "
    "commentary."
)


@dataclass(frozen=True, slots=True)
class Answer:
    """A successful answer."""

    text: str
    model: str
    usage: dict[str, Any] | None


def build_request_body(
    transcript: str,
    model: str = DEFAULT_ANSWER_MODEL,
    prompt: str = ANSWER_PROMPT,
) -> dict[str, Any]:
    """Return the chat-completions body that carries one transcript as text."""

    if not isinstance(transcript, str) or not transcript.strip():
        raise PermanentError("The transcript is empty.")
    if not isinstance(model, str) or not model:
        raise PermanentError("No model was given.")
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": transcript.strip()},
        ],
    }


def _content_text(content: Any) -> str | None:
    """Return the text of a message content field, or ``None``.

    OpenRouter returns a string for most models, but a list of typed parts is
    also valid, so both shapes are accepted.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        if parts:
            return "".join(parts)
    return None


def parse_response(status: int, body: bytes, model: str) -> Answer:
    """Turn one HTTP response into an :class:`Answer` or raise.

    The status handling matches :func:`voice_bridge_server.transcribe.
    parse_response`: auth and model failures are permanent, rate limits, 5xx and
    a malformed body are transient.
    """

    if status in (401, 403):
        raise PermanentError("OpenRouter rejected the API key.")
    if status == 404:
        raise PermanentError("OpenRouter does not serve that model.")
    if status == 429:
        raise TransientError("OpenRouter rate-limited the request.")
    if status >= 500:
        raise TransientError(f"OpenRouter returned HTTP {status}.")
    if status != 200:
        raise PermanentError(f"OpenRouter returned HTTP {status}.")

    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        raise TransientError("OpenRouter returned a response that is not JSON.")
    if not isinstance(payload, dict):
        raise TransientError("OpenRouter returned an unexpected response.")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TransientError("OpenRouter returned no choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise TransientError("OpenRouter returned an unexpected choice.")
    message = first.get("message")
    if not isinstance(message, dict):
        raise TransientError("OpenRouter returned no message.")

    text = _content_text(message.get("content"))
    if text is None:
        raise TransientError("OpenRouter returned no text.")

    usage = payload.get("usage")
    return Answer(
        text=text.strip(),
        model=payload.get("model") if isinstance(payload.get("model"), str) else model,
        usage=usage if isinstance(usage, dict) else None,
    )


def urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    """Default transport. Returns the status even for an error response."""

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        reason = error.reason
        raise OSError(reason if isinstance(reason, str) else "network error")
    except TimeoutError as error:
        raise OSError(str(error) or "the request timed out")


def answer_transcript(
    transcript: str,
    api_key: str,
    model: str = DEFAULT_ANSWER_MODEL,
    transport: Transport | None = None,
    timeout: float = 60.0,
) -> Answer:
    """Send one transcript to OpenRouter and return the answer.

    The API key travels only in the ``Authorization`` header. It is never put in
    the request body, a log line, or an error message.
    """

    if not isinstance(api_key, str) or not api_key:
        raise PermanentError("No OpenRouter API key was provided.")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise PermanentError("The timeout must be greater than zero.")

    payload = json.dumps(build_request_body(transcript, model)).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jamestkusek/kindle-voice-bridge",
        "X-Title": "Kindle Voice Bridge",
    }
    send = transport or urllib_transport
    try:
        status, response_body = send(ENDPOINT, headers, payload, float(timeout))
    except OSError as error:
        raise TransientError(f"The request failed: {error}.")
    return parse_response(status, response_body, model)
