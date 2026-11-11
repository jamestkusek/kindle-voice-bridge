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
* the page renders every value with DOM text nodes, never with ``innerHTML``.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)

from .audio import WavError, parse_wav
from .config import Settings

#: The exact ID format from ``docs/protocol.md`` §9.2. ``fullmatch`` is used so
#: no prefix, suffix, separator or path component can slip through.
RECORDING_ID_PATTERN = re.compile(r"rec_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{32}")

_SHA_PREFIX_CHARS = 12
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
    """Delete one stored recording, or raise a specific error."""

    path = recording_path(storage_dir, recording_id)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise RecordingNotFound(recording_id) from exc
    except OSError as exc:
        raise RecordingDeleteFailed(recording_id) from exc


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
<title>Kindle Voice Bridge - Recordings</title>
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
.meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.15rem 0.75rem;
  margin: 0.75rem 0;
  font-size: 0.9rem;
}
.meta dt { color: var(--muted); }
.meta dd { margin: 0; overflow-wrap: anywhere; }
audio { width: 100%; margin-top: 0.25rem; }
.download {
  display: inline-block;
  margin-top: 0.5rem;
  color: var(--accent);
  font-size: 0.9rem;
}
.warn { color: var(--danger); font-size: 0.9rem; }
.empty { color: var(--muted); text-align: center; padding: 2rem 0; }
</style>
</head>
<body>
<header>
  <h1>Recordings</h1>
  <div class="actions">
    <span id="status" role="status" aria-live="polite"></span>
    <button id="refresh" type="button">Refresh<kbd>R</kbd></button>
  </div>
</header>
<p class="hint">
  Local development review page. It is not authenticated, so use it only on a
  trusted LAN. Recordings are read from the configured storage directory.
</p>
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

  function audioUrl(id, download) {
    var url = "/recordings/" + encodeURIComponent(id) + ".wav";
    return download ? url + "?download=1" : url;
  }

  function render(items) {
    listEl.replaceChildren();
    if (!items.length) {
      var empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No recordings yet.";
      listEl.append(empty);
      return;
    }

    items.forEach(function (item) {
      var card = document.createElement("article");
      card.className = "recording";

      var head = document.createElement("div");
      head.className = "recording-head";

      var title = document.createElement("h2");
      title.textContent = item.id;
      head.append(title);

      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "Delete";
      remove.addEventListener("click", function () {
        deleteRecording(item.id);
      });
      head.append(remove);

      card.append(head);

      var meta = document.createElement("dl");
      meta.className = "meta";
      addMeta(meta, "Received", formatTime(item.received_at));
      addMeta(meta, "Duration", formatDuration(item.duration_seconds));
      addMeta(meta, "Sample rate", formatNumber(item.sample_rate_hz, " Hz"));
      addMeta(meta, "Channels", formatNumber(item.channels, ""));
      addMeta(meta, "Bit depth", formatNumber(item.bits_per_sample, "-bit"));
      addMeta(meta, "Size", formatBytes(item.size_bytes));
      addMeta(meta, "SHA-256", item.sha256_prefix || "\\u2014");
      card.append(meta);

      if (item.parse_error) {
        var warn = document.createElement("p");
        warn.className = "warn";
        warn.textContent =
          "This file could not be parsed: " + item.parse_error;
        card.append(warn);
      }

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

      listEl.append(card);
    });
  }

  function describeError(data, fallback) {
    if (data && data.error && data.error.message) {
      return data.error.message;
    }
    return fallback;
  }

  async function load() {
    statusEl.textContent = "Loading\\u2026";
    refreshBtn.disabled = true;
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
      statusEl.textContent =
        items.length + (items.length === 1 ? " recording" : " recordings");
    } catch (error) {
      statusEl.textContent = String(error.message || error);
    } finally {
      refreshBtn.disabled = false;
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
      statusEl.textContent = "Deleted.";
    } catch (error) {
      statusEl.textContent = String(error.message || error);
    }
  }

  refreshBtn.addEventListener("click", load);

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
