"""Speech-to-text for stored recordings through OpenRouter.

OpenRouter exposes no dedicated transcription endpoint. Audio is sent as an
``input_audio`` content part on a chat completion, and the model returns text.
``TRANSCRIBE_PROMPT`` asks for a verbatim transcript with no commentary, because
the models on that path are chat models and will otherwise add a preamble.

The module is pure apart from the injected transport. :func:`transcribe_wav`
takes a callable that performs one HTTP POST and returns ``(status, body)``, so
tests never touch the network. The default transport uses ``urllib`` and needs
no third-party package.

Failures are split into :class:`TransientError` (retry later) and
:class:`PermanentError` (do not retry), so the worker can back off without
guessing. Neither error ever contains the API key.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

#: A purpose-built ASR model, and the cheapest audio rate OpenRouter lists.
#: ``docs/transcription.md`` records the comparison that chose it.
DEFAULT_MODEL = "mistralai/voxtral-small-24b-2507"

#: The recordings are 8 kHz narrowband and may start with artifact, so the
#: prompt asks for a plain transcript rather than a cleaned-up summary.
TRANSCRIBE_PROMPT = (
    "Transcribe the speech in this recording verbatim. "
    "Output only the transcript text: no preamble, no speaker labels, no "
    "timestamps and no commentary. If the recording contains no speech, "
    "output nothing."
)

#: ``(url, headers, body, timeout) -> (status, response_body)``.
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


class TranscriptionError(Exception):
    """Base class for a failed transcription."""


class TransientError(TranscriptionError):
    """The request may succeed later: network, rate limit, or a 5xx."""


class PermanentError(TranscriptionError):
    """The request will not succeed without a change: auth, model, or audio."""


@dataclass(frozen=True, slots=True)
class Transcript:
    """A successful transcription."""

    text: str
    model: str
    usage: dict[str, Any] | None


def build_request_body(
    wav_bytes: bytes,
    model: str = DEFAULT_MODEL,
    prompt: str = TRANSCRIBE_PROMPT,
) -> dict[str, Any]:
    """Return the chat-completions body that carries one WAV as audio input."""

    if not isinstance(wav_bytes, (bytes, bytearray)) or not wav_bytes:
        raise PermanentError("The recording is empty.")
    if not isinstance(model, str) or not model:
        raise PermanentError("No model was given.")
    encoded = base64.b64encode(bytes(wav_bytes)).decode("ascii")
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": encoded, "format": "wav"},
                    },
                ],
            }
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


def parse_response(status: int, body: bytes, model: str) -> Transcript:
    """Turn one HTTP response into a :class:`Transcript` or raise."""

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
    return Transcript(
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


def transcribe_wav(
    wav_bytes: bytes,
    api_key: str,
    model: str = DEFAULT_MODEL,
    transport: Transport | None = None,
    timeout: float = 90.0,
) -> Transcript:
    """Send one WAV to OpenRouter and return its transcript.

    The API key travels only in the ``Authorization`` header. It is never put in
    the request body, a log line, or an error message.
    """

    if not isinstance(api_key, str) or not api_key:
        raise PermanentError("No OpenRouter API key was provided.")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise PermanentError("The timeout must be greater than zero.")

    payload = json.dumps(build_request_body(wav_bytes, model)).encode("utf-8")
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
