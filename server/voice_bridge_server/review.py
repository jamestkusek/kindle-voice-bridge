"""Development review UI for stored recordings.

These routes are a local development aid, not part of the upload contract in
``docs/protocol.md``. The upload endpoint, its authentication and the documented
wire format are untouched.

The review surface is deliberately unauthenticated and must only be reachable
on a trusted LAN during development. The page never receives the bearer token,
and no response echoes it.

Safety rules enforced here:

* a recording ID must match the server-generated format before it is used to
  build a path;
* the resolved path is checked to stay inside the configured storage directory;
* metadata is derived with :func:`voice_bridge_server.audio.parse_wav`, so there
  is no second WAV parser to keep in sync;
* the page renders every value with DOM text nodes, never with ``innerHTML``;
* the page polls only while a transcript or answer is still pending, and stops
  on its own.
"""

from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)

from .answers import answer_path, read_answer
from .audio import WavError, parse_wav
from .config import Settings
from .storage import RECORDING_ID_PATTERN
from .transcripts import read_sidecar, transcript_paths

_SHA_PREFIX_CHARS = 12
#: A sidecar is written by the receiver, but the page still caps what it will
#: echo so a corrupt or hand-edited file cannot bloat the listing.
_TRANSCRIPT_TEXT_LIMIT = 4000
_TRANSCRIPT_ERROR_LIMIT = 500
_ANSWER_TEXT_LIMIT = 4000
#: A stored clip was validated against the upload limit when it arrived. The
#: review listing only reports statistics, so it parses with a generous finite
#: limit instead of the current setting. A recording that is longer than the
#: current limit is still shown rather than disappearing from the page.
_REVIEW_DURATION_LIMIT_SECONDS = 3600.0

_DOWNLOAD_VALUES = frozenset({"1", "true", "yes"})


class InvalidRecordingId(ValueError):
    """The supplied value is not a server-generated recording ID."""


class RecordingNotFound(Exception):
    """No stored file exists for a well-formed recording ID."""


class RecordingDeleteFailed(Exception):
    """The stored file exists but could not be removed."""


def is_valid_recording_id(value: str) -> bool:
    """Return whether ``value`` is exactly a server-generated recording ID."""

    return isinstance(value, str) and RECORDING_ID_PATTERN.fullmatch(value) is not None


def recording_path(
    storage_dir: pathlib.Path, recording_id: str
) -> pathlib.Path:
    """Return the stored path for ``recording_id`` or raise.

    The ID is validated first, then the resolved path is compared with the
    storage directory as defence in depth. No user input is interpolated into a
    path unless it matches the fixed server-generated format.
    """

    if not is_valid_recording_id(recording_id):
        raise InvalidRecordingId(recording_id)

    path = storage_dir / f"{recording_id}.wav"
    try:
        resolved = path.resolve()
    except OSError as exc:  # pragma: no cover - defensive
        raise InvalidRecordingId(recording_id) from exc

    if resolved.parent != storage_dir or resolved.name != f"{recording_id}.wav":
        raise InvalidRecordingId(recording_id)
    return path


def list_recordings(storage_dir: pathlib.Path) -> list[dict[str, object]]:
    """Describe every stored recording, newest first.

    Files that are not server-generated recordings are ignored. A file that
    cannot be parsed is still listed, with ``parse_error`` set, so a broken clip
    is visible instead of silently missing.
    """

    try:
        entries = list(storage_dir.iterdir())
    except OSError:
        return []

    described: list[tuple[float, dict[str, object]]] = []
    for entry in entries:
        recording_id = entry.stem
        if entry.suffix != ".wav" or not is_valid_recording_id(recording_id):
            continue
        try:
            path = recording_path(storage_dir, recording_id)
        except InvalidRecordingId:
            # A symlink that resolves outside the storage directory is not a
            # recording this page may read.
            continue
        described_item = _describe_recording(path, recording_id)
        if described_item is None:
            continue
        described.append(described_item)

    described.sort(
        key=lambda pair: (pair[0], str(pair[1]["id"])), reverse=True
    )
    return [item for _, item in described]


def delete_recording(
    storage_dir: pathlib.Path, recording_id: str
) -> None:
    """Delete one stored recording and its sidecars, or raise a specific error.

    The transcript and answer sidecars belong to the recording, so leaving them
    behind would accumulate orphaned files in the storage directory.
    """

    path = recording_path(storage_dir, recording_id)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise RecordingNotFound(recording_id) from exc
    except OSError as exc:
        raise RecordingDeleteFailed(recording_id) from exc

    for sidecar in (*transcript_paths(path), answer_path(path)):
        try:
            sidecar.unlink()
        except OSError:
            # The recording is already gone. A sidecar that cannot be removed
            # is not a reason to report the delete as failed.
            pass


def register_review_routes(app: FastAPI, settings: Settings) -> None:
    """Attach the review page and its JSON/playback/delete routes."""

    storage_dir = settings.storage_dir

    @app.get("/", include_in_schema=False)
    def review_page() -> HTMLResponse:
        return HTMLResponse(
            _REVIEW_PAGE_HTML,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; media-src 'self'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'"
                ),
            },
        )

    @app.get("/api/recordings", include_in_schema=False)
    def list_api() -> JSONResponse:
        recordings = list_recordings(storage_dir)
        return _json_response(
            {"count": len(recordings), "recordings": recordings}
        )

    @app.get("/recordings/{recording_id}.wav", include_in_schema=False)
    def playback(recording_id: str, request: Request) -> Response:
        try:
            path = recording_path(storage_dir, recording_id)
        except InvalidRecordingId:
            return _error_response(
                400,
                "invalid_recording_id",
                "Recording ID is not in the expected format.",
            )

        if not path.is_file():
            return _error_response(
                404, "not_found", "Recording not found."
            )

        disposition = (
            "attachment"
            if request.query_params.get("download") in _DOWNLOAD_VALUES
            else "inline"
        )
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=f"{recording_id}.wav",
            content_disposition_type=disposition,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.delete("/api/recordings/{recording_id}", include_in_schema=False)
    def delete_api(recording_id: str) -> JSONResponse:
        try:
            delete_recording(storage_dir, recording_id)
        except InvalidRecordingId:
            return _error_response(
                400,
                "invalid_recording_id",
                "Recording ID is not in the expected format.",
            )
        except RecordingNotFound:
            return _error_response(
                404, "not_found", "Recording not found."
            )
        except RecordingDeleteFailed:
            return _error_response(
                500, "storage_error", "Could not delete recording."
            )

        recordings = list_recordings(storage_dir)
        return _json_response(
            {
                "deleted": recording_id,
                "count": len(recordings),
                "recordings": recordings,
            }
        )


def _describe_recording(
    path: pathlib.Path, recording_id: str
) -> tuple[float, dict[str, object]] | None:
    try:
        stat = path.stat()
        body = path.read_bytes()
    except OSError:
        return None

    try:
        received_at: str | None = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):  # pragma: no cover - odd mtime
        received_at = None

    item: dict[str, object] = {
        "id": recording_id,
        "received_at": received_at,
        "size_bytes": len(body),
        "sha256_prefix": hashlib.sha256(body).hexdigest()[
            :_SHA_PREFIX_CHARS
        ],
        "duration_seconds": None,
        "sample_rate_hz": None,
        "channels": None,
        "bits_per_sample": None,
        "parse_error": None,
        "transcript": _transcript_summary(path),
        "answer": _answer_summary(path),
    }

    try:
        info = parse_wav(
            body, max_duration_seconds=_REVIEW_DURATION_LIMIT_SECONDS
        )
    except WavError as exc:
        item["parse_error"] = exc.code
    except Exception:  # pragma: no cover - parse_wav is defensive already
        item["parse_error"] = "invalid_wav"
    else:
        item["duration_seconds"] = info.duration_seconds
        item["sample_rate_hz"] = info.sample_rate_hz
        item["channels"] = info.channels
        item["bits_per_sample"] = info.bits_per_sample

    return stat.st_mtime, item


def _transcript_summary(path: pathlib.Path) -> dict[str, object] | None:
    """Return the transcript sidecar for one recording, or ``None``.

    The sidecar is written by the receiver, but every field is re-typed and
    capped here so the page never renders an unexpected shape.
    """

    sidecar = read_sidecar(path)
    if sidecar is None:
        return None

    def text(value: object, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        return value if len(value) <= limit else value[: limit - 1] + "…"

    usage = sidecar.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    attempts = sidecar.get("attempts")
    status = sidecar.get("status")
    return {
        "status": status if status in ("ok", "error") else "unknown",
        "text": text(sidecar.get("text"), _TRANSCRIPT_TEXT_LIMIT) or "",
        "model": text(sidecar.get("model"), 120),
        "attempts": attempts if isinstance(attempts, int) else None,
        "error": text(sidecar.get("error"), _TRANSCRIPT_ERROR_LIMIT),
        "retryable": sidecar.get("retryable") is True,
        "cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        "updated_at": text(sidecar.get("updated_at"), 40),
    }


def _answer_summary(path: pathlib.Path) -> dict[str, object] | None:
    """Return the answer sidecar for one recording, or ``None``.

    The sidecar is written by the receiver, but every field is re-typed and
    capped here so the page never renders an unexpected shape.
    """

    sidecar = read_answer(path)
    if sidecar is None:
        return None

    def text(value: object, limit: int) -> str | None:
        if not isinstance(value, str):
            return None
        return value if len(value) <= limit else value[: limit - 1] + "…"

    usage = sidecar.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    status = sidecar.get("status")
    return {
        "status": status if status in ("ok", "error", "skipped") else "unknown",
        "text": text(sidecar.get("text"), _ANSWER_TEXT_LIMIT) or "",
        "model": text(sidecar.get("model"), 120),
        "error": text(sidecar.get("error"), _TRANSCRIPT_ERROR_LIMIT),
        "retryable": sidecar.get("retryable") is True,
        "cost_usd": cost
        if isinstance(cost, (int, float)) and not isinstance(cost, bool)
        else None,
        "updated_at": text(sidecar.get("updated_at"), 40),
    }


def _json_response(payload: dict[str, object]) -> JSONResponse:
    return JSONResponse(
        payload, headers={"Cache-Control": "no-store"}
    )


def _error_response(
    status_code: int, code: str, message: str
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


_REVIEW_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kindle Voice LLM Interface</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f5f6f8;
  --fg: #1b1f24;
  --muted: #5b6572;
  --card: #ffffff;
  --border: #d9dee5;
  --accent: #0b6bcb;
  --danger: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a;
    --fg: #e8eaed;
    --muted: #9aa4af;
    --card: #1c2024;
    --border: #2c3238;
    --accent: #6cb6ff;
    --danger: #ff8a80;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem;
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--fg);
}
header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  max-width: 960px;
  margin: 0 auto 0.5rem;
}
h1 { font-size: 1.35rem; margin: 0; }
.actions { display: flex; align-items: center; gap: 0.75rem; }
#status { color: var(--muted); font-size: 0.9rem; }
button {
  font: inherit;
  padding: 0.35rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--card);
  color: var(--fg);
  cursor: pointer;
}
button:hover:not(:disabled) { border-color: var(--accent); }
button:disabled { opacity: 0.6; cursor: default; }
button.danger { color: var(--danger); }
kbd {
  font: 0.8em ui-monospace, monospace;
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 0.25rem;
  margin-left: 0.25rem;
  color: var(--muted);
}
.hint {
  max-width: 960px;
  margin: 0 auto 1.25rem;
  color: var(--muted);
  font-size: 0.85rem;
}
main { max-width: 960px; margin: 0 auto; display: grid; gap: 1rem; }
.recording {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem;
}
.recording-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem;
}
.recording h2 {
  font: 0.85rem/1.4 ui-monospace, "Cascadia Mono", monospace;
  margin: 0;
  overflow-wrap: anywhere;
}
.received { margin: 0.15rem 0 0; color: var(--muted); font-size: 0.8rem; }
.section { margin-top: 0.85rem; }
.section h3 {
  margin: 0 0 0.2rem;
  color: var(--muted);
  font-size: 0.75rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}
.answer { margin: 0; font-size: 1.15rem; overflow-wrap: anywhere; }
.transcript { margin: 0; font-size: 1rem; overflow-wrap: anywhere; }
.note {
  margin: 0.2rem 0 0;
  color: var(--muted);
  font-size: 0.8rem;
  overflow-wrap: anywhere;
}
audio { width: 100%; margin-top: 0.9rem; }
.download {
  display: inline-block;
  margin-top: 0.5rem;
  color: var(--accent);
  font-size: 0.9rem;
}
.warn { color: var(--danger); font-size: 0.9rem; }
details {
  margin-top: 0.85rem;
  border-top: 1px solid var(--border);
  padding-top: 0.6rem;
}
summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; }
summary:hover { color: var(--accent); }
.meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.15rem 0.75rem;
  margin: 0.6rem 0 0;
  font-size: 0.85rem;
}
.meta dt { color: var(--muted); }
.meta dd { margin: 0; overflow-wrap: anywhere; }
.detail-group { margin-top: 0.75rem; }
.detail-group h4 {
  margin: 0 0 0.2rem;
  color: var(--muted);
  font-size: 0.8rem;
}
.empty { color: var(--muted); text-align: center; padding: 2rem 0; }
</style>
</head>
<body>
<header>
  <h1>Kindle Voice LLM Interface</h1>
  <div class="actions">
    <span id="status" role="status" aria-live="polite"></span>
    <button id="refresh" type="button">Refresh<kbd>R</kbd></button>
  </div>
</header>
<main id="list" aria-live="polite"></main>
<noscript>
  <p class="hint">This page needs JavaScript to list and play recordings.</p>
</noscript>
<script>
(function () {
  "use strict";

  var listEl = document.getElementById("list");
  var statusEl = document.getElementById("status");
  var refreshBtn = document.getElementById("refresh");
  var pollTimer = null;
  var cards = new Map();

  function formatBytes(value) {
    if (typeof value !== "number" || !isFinite(value)) return "\\u2014";
    if (value < 1024) return value + " B";
    var units = ["KiB", "MiB", "GiB"];
    var size = value;
    var unit = "B";
    for (var i = 0; i < units.length && size >= 1024; i += 1) {
      size /= 1024;
      unit = units[i];
    }
    return size.toFixed(size >= 10 ? 0 : 1) + " " + unit;
  }

  function formatDuration(value) {
    if (typeof value !== "number" || !isFinite(value)) return "\\u2014";
    return value.toFixed(3) + " s";
  }

  function formatTime(value) {
    if (typeof value !== "string") return "\\u2014";
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) return value;
    return parsed.toLocaleString();
  }

  function formatNumber(value, suffix) {
    if (typeof value !== "number" || !isFinite(value)) return "\\u2014";
    return value + suffix;
  }

  function addMeta(container, label, value) {
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = value;
    container.append(dt, dd);
  }

  function formatCost(value) {
    if (typeof value !== "number" || !isFinite(value)) return null;
    if (value > 0 && value < 0.01) return "$" + value.toFixed(5);
    return "$" + value.toFixed(2);
  }

  function metadataNote(record) {
    var parts = [];
    if (record.model) parts.push(record.model);
    if (typeof record.attempts === "number") {
      parts.push(
        record.attempts +
          (record.attempts === 1 ? " attempt" : " attempts")
      );
    }
    var cost = formatCost(record.cost_usd);
    if (cost) parts.push(cost);
    return parts.join(" · ");
  }

  function answerNote(answer) {
    var parts = [];
    if (answer.model) parts.push(answer.model);
    var cost = formatCost(answer.cost_usd);
    if (cost) parts.push(cost);
    return parts.join(" · ");
  }

  function isFresh(value, windowMs) {
    var parsed = Date.parse(value);
    var limit = typeof windowMs === "number" ? windowMs : 60000;
    return isFinite(parsed) && Date.now() - parsed < limit;
  }

  // A transcript is pending until its sidecar settles. A recording with no
  // sidecar only counts as pending while it is fresh, so a storage directory
  // from before transcription was enabled does not poll forever.
  function isTranscriptPending(item) {
    if (item.transcript) {
      return item.transcript.status !== "ok" &&
        item.transcript.retryable === true;
    }
    return isFresh(item.received_at);
  }

  // The answer is written just after a successful transcript, so a fresh
  // transcript with speech and no answer sidecar is still in flight.
  function isAnswerPending(item) {
    if (!item.transcript || item.transcript.status !== "ok") return false;
    if (!item.transcript.text) return false;
    if (item.answer) return false;
    // The answer call has a 60-second timeout, so allow for that plus overhead.
    return isFresh(item.transcript.updated_at, 120000);
  }

  function addSection(card, title, className, text, note) {
    var section = document.createElement("section");
    section.className = "section";
    var heading = document.createElement("h3");
    heading.textContent = title;
    section.append(heading);
    var body = document.createElement("p");
    body.className = className;
    body.textContent = text;
    section.append(body);
    if (note) {
      var noteEl = document.createElement("p");
      noteEl.className = "note";
      noteEl.textContent = note;
      section.append(noteEl);
    }
    card.append(section);
  }

  function addDetailGroup(container, title, record) {
    if (!record) return;
    var group = document.createElement("div");
    group.className = "detail-group";
    var heading = document.createElement("h4");
    heading.textContent = title;
    group.append(heading);
    var meta = document.createElement("dl");
    meta.className = "meta";
    addMeta(meta, "Status", record.status);
    if (record.model) addMeta(meta, "Model", record.model);
    if (typeof record.attempts === "number") {
      addMeta(meta, "Attempts", String(record.attempts));
    }
    var cost = formatCost(record.cost_usd);
    if (cost) addMeta(meta, "Cost", cost);
    if (record.updated_at) {
      addMeta(meta, "Updated", formatTime(record.updated_at));
    }
    if (record.error) addMeta(meta, "Error", record.error);
    group.append(meta);
    container.append(group);
  }

  function buildDetails(item) {
    var details = document.createElement("details");
    var summary = document.createElement("summary");
    summary.textContent = "Details";
    details.append(summary);
    fillDetails(details, item);
    return details;
  }

  function fillDetails(details, item) {
    while (details.lastChild !== details.firstChild) {
      details.removeChild(details.lastChild);
    }
    var meta = document.createElement("dl");
    meta.className = "meta";
    addMeta(meta, "Received", formatTime(item.received_at));
    addMeta(meta, "Duration", formatDuration(item.duration_seconds));
    addMeta(meta, "Sample rate", formatNumber(item.sample_rate_hz, " Hz"));
    addMeta(meta, "Channels", formatNumber(item.channels, ""));
    addMeta(meta, "Bit depth", formatNumber(item.bits_per_sample, "-bit"));
    addMeta(meta, "Size", formatBytes(item.size_bytes));
    addMeta(meta, "SHA-256", item.sha256_prefix || "\u2014");
    details.append(meta);

    addDetailGroup(details, "Transcription", item.transcript);
    addDetailGroup(details, "Answer", item.answer);
  }

  function audioUrl(id, download) {
    var url = "/recordings/" + encodeURIComponent(id) + ".wav";
    return download ? url + "?download=1" : url;
  }

  function fillSections(container, item) {
    container.replaceChildren();
    if (item.parse_error) {
      var warn = document.createElement("p");
      warn.className = "warn";
      warn.textContent =
        "This file could not be parsed: " + item.parse_error;
      container.append(warn);
    }

    if (item.answer) {
      if (item.answer.status === "ok") {
        addSection(
          container,
          "Answer",
          "answer",
          item.answer.text || "(no answer text)",
          answerNote(item.answer)
        );
      } else if (item.answer.status === "skipped") {
        addSection(
          container,
          "Answer",
          "note",
          "No answer: the transcript was empty."
        );
      } else {
        addSection(
          container,
          "Answer",
          "warn",
          "Answer failed: " +
            (item.answer.error || "unknown error")
        );
      }
    } else if (isAnswerPending(item)) {
      addSection(container, "Answer", "note", "Waiting for answer…");
    }

    if (item.transcript) {
      if (item.transcript.status === "ok") {
        addSection(
          container,
          "Transcript",
          "transcript",
          item.transcript.text || "(no speech detected)",
          metadataNote(item.transcript)
        );
      } else {
        addSection(
          container,
          "Transcript",
          "warn",
          "Transcription failed: " +
            (item.transcript.error || "unknown error")
        );
      }
    } else if (isTranscriptPending(item)) {
      addSection(container, "Transcript", "note", "Transcribing…");
    } else {
      addSection(container, "Transcript", "note", "No transcript.");
    }
  }

  function createCard(item) {
    var card = document.createElement("article");
    card.className = "recording";

    var head = document.createElement("div");
    head.className = "recording-head";

    var identity = document.createElement("div");
    var title = document.createElement("h2");
    title.textContent = item.id;
    identity.append(title);
    var received = document.createElement("p");
    received.className = "received";
    received.textContent = "Received " + formatTime(item.received_at);
    identity.append(received);
    head.append(identity);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "Delete";
    remove.addEventListener("click", function () {
      deleteRecording(item.id);
    });
    head.append(remove);
    card.append(head);

    var sections = document.createElement("div");
    sections.className = "sections";
    fillSections(sections, item);
    card.append(sections);

    var audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = audioUrl(item.id, false);
    card.append(audio);

    var download = document.createElement("a");
    download.className = "download";
    download.href = audioUrl(item.id, true);
    download.textContent = "Download";
    card.append(download);

    var details = buildDetails(item);
    card.append(details);

    return {
      card: card,
      received: received,
      sections: sections,
      details: details,
      signature: JSON.stringify(item),
    };
  }

  function updateCard(entry, item) {
    entry.received.textContent = "Received " + formatTime(item.received_at);
    fillSections(entry.sections, item);
    fillDetails(entry.details, item);
    entry.signature = JSON.stringify(item);
  }

  function render(items) {
    var empty = listEl.querySelector(".empty");
    if (empty) empty.remove();

    var seen = new Set();
    items.forEach(function (item, index) {
      seen.add(item.id);
      var entry = cards.get(item.id);
      var signature = JSON.stringify(item);
      if (!entry) {
        entry = createCard(item);
        cards.set(item.id, entry);
      } else if (entry.signature !== signature) {
        updateCard(entry, item);
      }
      if (listEl.children[index] !== entry.card) {
        listEl.insertBefore(entry.card, listEl.children[index] || null);
      }
    });

    var stale = [];
    cards.forEach(function (entry, id) {
      if (!seen.has(id)) stale.push(id);
    });
    stale.forEach(function (id) {
      cards.get(id).card.remove();
      cards.delete(id);
    });

    if (!items.length) {
      var message = document.createElement("p");
      message.className = "empty";
      message.textContent = "No recordings yet.";
      listEl.append(message);
    }
  }

  function describeError(data, fallback) {
    if (data && data.error && data.error.message) {
      return data.error.message;
    }
    return fallback;
  }

  function schedulePoll(delayMs) {
    if (pollTimer !== null) {
      window.clearTimeout(pollTimer);
    }
    pollTimer = window.setTimeout(function () {
      pollTimer = null;
      load(true);
    }, delayMs || 2000);
  }

  async function load(silent) {
    if (!silent) {
      statusEl.textContent = "Loading\\u2026";
      refreshBtn.disabled = true;
    }
    try {
      var response = await fetch("/api/recordings", { cache: "no-store" });
      var data = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(
          describeError(data, "Could not load recordings (" + response.status + ").")
        );
      }
      var items = Array.isArray(data.recordings) ? data.recordings : [];
      render(items);
      schedulePoll(2000);
      statusEl.textContent =
        items.length + (items.length === 1 ? " recording" : " recordings");
    } catch (error) {
      statusEl.textContent = String(error.message || error);
      schedulePoll(5000);
    } finally {
      if (!silent) refreshBtn.disabled = false;
    }
  }

  async function deleteRecording(id) {
    if (!window.confirm("Delete " + id + "? This cannot be undone.")) {
      return;
    }
    statusEl.textContent = "Deleting\\u2026";
    try {
      var response = await fetch(
        "/api/recordings/" + encodeURIComponent(id),
        { method: "DELETE" }
      );
      var data = await response.json().catch(function () { return null; });
      if (!response.ok) {
        throw new Error(
          describeError(data, "Delete failed (" + response.status + ").")
        );
      }
      var items = Array.isArray(data.recordings) ? data.recordings : [];
      render(items);
      schedulePoll(2000);
      statusEl.textContent = "Deleted.";
    } catch (error) {
      statusEl.textContent = String(error.message || error);
    }
  }

  refreshBtn.addEventListener("click", function () {
    load();
  });

  document.addEventListener("keydown", function (event) {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey ||
        event.altKey) {
      return;
    }
    if (event.key !== "r" && event.key !== "R") return;
    var target = event.target;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
    event.preventDefault();
    load();
  });

  load();
})();
</script>
</body>
</html>
"""
