"""Per-ask trace files and the stage log.

One ask produces one trace file beside the WAV:

    <storage>/<recording_id>.trace.json

The file carries the whole observability record for that ask: the decoded
context, the book match, the stage timings, the resolved passage, the exact
model messages, the answer length and the cost. It is rewritten after each
stage through a fsynced temporary file and an atomic rename, so the debug page
never reads a half-written trace.

Logs are deliberately poorer than the trace. A stage log line carries the stage
name, the recording id, the duration, the outcome and scalar fields only. Prompt
text, passages and free-form details stay in the trace file. That rule is
enforced in :func:`log_stage`, not by its callers.

The store owns the filesystem workflow only. It does not call a model, parse
audio or validate a WAV. ``raw`` and ``text`` are the retention switches from
``docs/plans/milestone-2-context-observability.md``: ``raw=False`` drops the
model response text, ``text=False`` drops prompts, the resolved passage and the
context text fields. A dropped field is written as an empty value so the schema
stays stable for the reader.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import json
import logging
import os
import pathlib
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Any

#: Schema version of the trace document.
TRACE_VERSION = 1

#: Suffix of one trace file. The recording id is the stem.
TRACE_SUFFIX = ".trace.json"

#: Longest string value kept inside a trace. A longer value is replaced by a
#: short marker.
MAX_STRING_CHARS = 2048

#: Largest trace document, including the trailing newline.
MAX_TRACE_BYTES = 16384

#: Number of formatted log lines kept for ``GET /api/logs``.
LOG_BUFFER_SIZE = 500

#: Trace sections that :meth:`TraceStore.set` accepts.
SECTIONS = ("context", "book", "resolve", "llm", "answer")

#: Terminal and non-terminal outcomes of one ask.
OUTCOMES = ("pending", "ok", "failed")

#: Field names whose value is text. :func:`log_stage` never logs these.
_TEXT_FIELDS = frozenset(
    {
        "detail",
        "prompt",
        "passage",
        "text",
        "response_text",
        "system",
        "user",
        "highlight",
        "parsed",
        "messages",
        "transcript",
        "question",
    }
)

#: Context payload fields that hold book or highlight text.
_CONTEXT_TEXT_KEYS = frozenset({"highlight", "chapter", "title", "authors"})

#: Longest scalar string kept in a log line.
_LOG_VALUE_CHARS = 120

#: Attempts at one file operation when another process has the file open.
_RETRY_ATTEMPTS = 10

#: Pause between those attempts, in seconds.
_RETRY_DELAY_SECONDS = 0.02

#: Error codes that mean a sharing violation, not a missing file.
_SHARING_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EBUSY})
_SHARING_WINERRORS = frozenset({5, 32, 33})

_LOGGER_NAME = "voice_bridge_server"
_STAGE_LOGGER = logging.getLogger(f"{_LOGGER_NAME}.trace")
_TEMP_PREFIX = ".trace-"
_TEMP_SUFFIX = ".part"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: Formatted log lines, newest last. Filled by the handler that
#: :func:`configure_logging` installs.
LOG_BUFFER: collections.deque[str] = collections.deque(maxlen=LOG_BUFFER_SIZE)


class TraceStore:
    """Read and write one trace file per recording.

    ``raw`` keeps the model response text. ``text`` keeps prompts, the resolved
    passage and the context text fields. Both default to the documented
    receiver defaults. Every write takes one lock so a read-modify-write cycle
    cannot interleave with another thread.
    """

    def __init__(
        self,
        storage_dir: str | os.PathLike[str],
        *,
        raw: bool = False,
        text: bool = True,
    ) -> None:
        self._storage_dir = pathlib.Path(storage_dir)
        self._raw = bool(raw)
        self._text = bool(text)
        self._lock = threading.RLock()

    @property
    def storage_dir(self) -> pathlib.Path:
        """Directory that holds the trace files."""

        return self._storage_dir

    def path(self, recording_id: str) -> pathlib.Path:
        """Return the trace path for ``recording_id``.

        Raises ``ValueError`` when the id could address another directory.
        """

        if not _is_safe_recording_id(recording_id):
            raise ValueError("Invalid recording id.")
        return self._storage_dir / f"{recording_id}{TRACE_SUFFIX}"

    def start(
        self,
        recording_id: str,
        *,
        origin: str,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Write the header of a new trace.

        ``context`` may supply ``header_bytes``, ``parsed`` and
        ``rejected_reason``. An existing trace for the same id is replaced.
        """

        if not isinstance(origin, str) or not origin:
            raise ValueError("origin must be a non-empty string")
        trace = _new_trace(recording_id, origin)
        if context is not None:
            if not isinstance(context, Mapping):
                raise ValueError("context must be a mapping")
            trace["context"] = _merge(trace["context"], dict(context))
        with self._lock:
            self._write(recording_id, trace)

    def set(self, recording_id: str, section: str, value: Any) -> None:
        """Replace one trace section. A mapping is merged into the section."""

        if section not in SECTIONS:
            raise ValueError(f"Unknown trace section: {section!r}.")
        with self._lock:
            trace = self._require(recording_id)
            trace[section] = _merge(trace.get(section), value)
            self._write(recording_id, trace)

    def stage(
        self,
        recording_id: str,
        name: str,
        *,
        outcome: str = "ok",
        detail: Any = None,
        duration_ms: float | None = None,
    ) -> None:
        """Append one stage entry and rewrite the trace."""

        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        entry = {
            "name": name,
            "started_at": utc_now(),
            "dur_ms": _duration(duration_ms),
            "outcome": outcome,
            "detail": {} if detail is None else detail,
        }
        with self._lock:
            trace = self._require(recording_id)
            stages = trace.get("stages")
            if not isinstance(stages, list):
                stages = []
                trace["stages"] = stages
            stages.append(entry)
            self._write(recording_id, trace)

    @contextlib.contextmanager
    def timed(
        self, recording_id: str, name: str, *, outcome: str = "ok"
    ) -> Iterator[None]:
        """Time a block and record it as one stage.

        An exception is recorded with ``outcome="failed"`` and re-raised. A
        trace write failure never masks the original exception.
        """

        started = time.perf_counter()
        try:
            yield
        except BaseException as error:
            duration = _elapsed_ms(started)
            try:
                self.stage(
                    recording_id,
                    name,
                    outcome="failed",
                    detail={"error": type(error).__name__},
                    duration_ms=duration,
                )
            except Exception:  # pragma: no cover - keep the original error
                pass
            raise
        else:
            self.stage(
                recording_id, name, outcome=outcome, duration_ms=_elapsed_ms(started)
            )

    def set_outcome(self, recording_id: str, outcome: str) -> None:
        """Set the outcome of one ask."""

        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown outcome: {outcome!r}.")
        with self._lock:
            trace = self._require(recording_id)
            trace["outcome"] = outcome
            self._write(recording_id, trace)

    def read(self, recording_id: str) -> dict[str, Any] | None:
        """Return the parsed trace, or ``None`` when it is missing or bad.

        A transient sharing violation is retried, because the atomic rename in
        another thread or process can hold the file open for a moment.
        """

        with self._lock:
            return _read_file(self.path(recording_id))

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return summaries, newest first. Corrupt files are skipped."""

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            return []

        entries: list[tuple[str, int, dict[str, Any]]] = []
        for path in self._storage_dir.glob(f"*{TRACE_SUFFIX}"):
            # Same retry path as read(), so a busy file is not a missing row.
            trace = _read_file(path)
            if trace is None:
                continue
            try:
                modified = path.stat().st_mtime_ns
            except OSError:  # pragma: no cover - file vanished during the scan
                modified = 0
            created = trace.get("created_at")
            entries.append(
                (
                    created if isinstance(created, str) else "",
                    modified,
                    _summary(trace),
                )
            )
        entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [summary for _, _, summary in entries[:limit]]

    # Internals ------------------------------------------------------------

    def _require(self, recording_id: str) -> dict[str, Any]:
        trace = self.read(recording_id)
        if trace is None:
            raise FileNotFoundError(f"No trace for {recording_id}.")
        return trace

    def _write(self, recording_id: str, trace: dict[str, Any]) -> None:
        hidden = _hide_text(trace, raw=self._raw, text=self._text)
        capped = _cap_strings(hidden)
        _write_atomic(self.path(recording_id), _fit(capped))


def log_stage(
    name: str,
    recording_id: str | None = None,
    duration_ms: float | None = None,
    outcome: str = "ok",
    **fields: Any,
) -> None:
    """Write one stage log line.

    Only scalar fields are logged, and the text field names are dropped even
    when their value is scalar. A log line never carries a prompt, a passage or
    a detail object.
    """

    parts = [f"stage={_log_value(name)}"]
    if recording_id is not None:
        parts.append(f"id={_log_value(recording_id)}")
    if duration_ms is not None:
        parts.append(f"dur_ms={_log_value(_duration(duration_ms))}")
    parts.append(f"outcome={_log_value(outcome)}")
    for key, value in fields.items():
        if key in _TEXT_FIELDS or not _is_scalar(value):
            continue
        parts.append(f"{key}={_log_value(value)}")
    _STAGE_LOGGER.info(" ".join(parts))


def configure_logging(level: str = "") -> None:
    """Install the log buffer handler once and set the log level.

    An empty ``level`` falls back to ``KVB_LOG_LEVEL``, then to ``INFO``. The
    handler keeps the last :data:`LOG_BUFFER_SIZE` formatted lines.
    """

    resolved = _log_level(level or os.environ.get("KVB_LOG_LEVEL", ""))
    root = logging.getLogger()
    if _BUFFER_HANDLER not in root.handlers:
        root.addHandler(_BUFFER_HANDLER)
    root.setLevel(resolved)
    logging.getLogger(_LOGGER_NAME).setLevel(resolved)


def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class _BufferHandler(logging.Handler):
    """Append formatted log lines to :data:`LOG_BUFFER`."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:  # pragma: no cover - a logging failure is not fatal
            self.handleError(record)


_BUFFER_HANDLER = _BufferHandler()
_BUFFER_HANDLER.setFormatter(logging.Formatter(_LOG_FORMAT))


def _new_trace(recording_id: str, origin: str) -> dict[str, Any]:
    return {
        "v": TRACE_VERSION,
        "id": recording_id,
        "origin": origin,
        "created_at": utc_now(),
        "outcome": "pending",
        "context": {"header_bytes": 0, "parsed": {}, "rejected_reason": None},
        "book": {"key": "", "match": "none"},
        "stages": [],
        "resolve": {"rung": "none", "reason": "", "passage": ""},
        "llm": {
            "model": "",
            "prompt": {"system": "", "user": ""},
            "usage": {},
            "cost_usd": None,
            "cost_source": "none",
            "response_text": "",
        },
        "answer": {"chars_declared": 0, "chars_sent": 0},
    }


def _hide_text(
    trace: dict[str, Any], *, raw: bool, text: bool
) -> dict[str, Any]:
    """Drop the retained text that the store is configured to hide."""

    llm = trace.get("llm")
    if isinstance(llm, dict):
        if not text or not isinstance(llm.get("prompt"), dict):
            llm["prompt"] = {"system": "", "user": ""}
        if not raw:
            llm["response_text"] = ""

    resolve = trace.get("resolve")
    if isinstance(resolve, dict) and not text:
        resolve["passage"] = ""

    context = trace.get("context")
    if isinstance(context, dict) and not text:
        parsed = context.get("parsed")
        if isinstance(parsed, dict):
            context["parsed"] = _strip_context_text(parsed)
    return trace


def _strip_context_text(parsed: dict[str, Any]) -> dict[str, Any]:
    """Replace the text fields of a parsed context payload with empty values."""

    stripped: dict[str, Any] = {}
    for key, value in parsed.items():
        if key in _CONTEXT_TEXT_KEYS:
            stripped[key] = [] if isinstance(value, list) else ""
            continue
        if isinstance(value, dict):
            stripped[key] = {
                inner: (
                    []
                    if inner in _CONTEXT_TEXT_KEYS and isinstance(item, list)
                    else ("" if inner in _CONTEXT_TEXT_KEYS else item)
                )
                for inner, item in value.items()
            }
            continue
        stripped[key] = value
    return stripped


def _merge(current: Any, value: Any) -> Any:
    """Merge one mapping over another, one level of nesting deep."""

    if not isinstance(current, dict) or not isinstance(value, Mapping):
        return value
    merged = dict(current)
    for key, item in value.items():
        merged[key] = _merge(merged.get(key), item)
    return merged


def _cap_strings(value: Any) -> Any:
    """Replace every string longer than :data:`MAX_STRING_CHARS`."""

    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            return _truncated(len(value))
        return value
    if isinstance(value, dict):
        return {key: _cap_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cap_strings(item) for item in value]
    return value


def _fit(trace: dict[str, Any]) -> str:
    """Serialise ``trace`` within :data:`MAX_TRACE_BYTES`, newline included.

    The longest values are replaced by markers first, so the stage list and the
    identity of the ask survive when the text fields do not.
    """

    body = _serialise(trace)
    if _byte_size(body) <= MAX_TRACE_BYTES:
        return body + "\n"
    for length, holder, key in _string_slots(trace):
        holder[key] = _truncated(length)
        body = _serialise(trace)
        if _byte_size(body) <= MAX_TRACE_BYTES:
            return body + "\n"
    return _serialise(_minimum_trace(trace)) + "\n"


def _serialise(trace: dict[str, Any]) -> str:
    return json.dumps(trace, sort_keys=True, default=str)


def _byte_size(body: str) -> int:
    """Size of the document as written, including the trailing newline."""

    return len(body.encode("utf-8")) + 1


def _string_slots(value: Any) -> list[tuple[int, dict[str, Any], str]]:
    """Return the replaceable strings as ``(length, holder, key)``, longest first.

    A string is replaceable only when its marker would be shorter than the
    value itself, so every replacement shrinks the document.
    """

    slots: list[tuple[int, dict[str, Any], str]] = []
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if isinstance(item, str):
                    if _is_marker(item) or len(item) <= len(_truncated(len(item))):
                        continue
                    slots.append((len(item), current, key))
                else:
                    stack.append(item)
        elif isinstance(current, list):
            stack.extend(item for item in current if not isinstance(item, str))
    slots.sort(key=lambda slot: slot[0], reverse=True)
    return slots


def _minimum_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Return the smallest useful trace, used when nothing else fits."""

    return {
        "v": TRACE_VERSION,
        "id": trace.get("id"),
        "origin": trace.get("origin"),
        "created_at": trace.get("created_at"),
        "outcome": trace.get("outcome"),
        "stages": [],
        "truncated": f"trace exceeds {MAX_TRACE_BYTES} bytes",
    }


def _truncated(length: int) -> str:
    return f"[truncated {length} chars]"


def _is_marker(value: str) -> bool:
    return value.startswith("[truncated ") and value.endswith(" chars]")


def _summary(trace: dict[str, Any]) -> dict[str, Any]:
    """Return the listing row for one trace."""

    stages = trace.get("stages")
    last = stages[-1] if isinstance(stages, list) and stages else {}
    if not isinstance(last, dict):
        last = {}
    llm = trace.get("llm") if isinstance(trace.get("llm"), dict) else {}
    title, page = _context_book(trace)
    return {
        "id": trace.get("id"),
        "origin": trace.get("origin"),
        "outcome": trace.get("outcome"),
        "created_at": trace.get("created_at"),
        "stage": last.get("name"),
        "stage_outcome": last.get("outcome"),
        "total_ms": _total_ms(stages),
        "book_title": title,
        "book_page": page,
        "cost_usd": llm.get("cost_usd"),
    }


def _context_book(trace: dict[str, Any]) -> tuple[str | None, int | None]:
    """Return the book title and page recorded in the context payload."""

    context = trace.get("context")
    parsed = context.get("parsed") if isinstance(context, dict) else None
    if not isinstance(parsed, dict):
        return None, None
    book = parsed.get("book")
    title = book.get("title") if isinstance(book, dict) else None
    position = parsed.get("pos")
    page = position.get("page") if isinstance(position, dict) else None
    return (
        title if isinstance(title, str) and title else None,
        page if isinstance(page, int) and not isinstance(page, bool) else None,
    )


def _total_ms(stages: Any) -> float:
    """Sum the stage durations. A missing duration adds nothing."""

    total = 0.0
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            duration = stage.get("dur_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                total += float(duration)
    return round(total, 3)


def _read_file(path: pathlib.Path) -> dict[str, Any] | None:
    """Read one trace file, retrying a transient sharing violation.

    A missing or corrupt file returns ``None``. Only ``PermissionError`` is
    retried, because only that one is usually short-lived on Windows.
    """

    for attempt in range(_RETRY_ATTEMPTS):
        try:
            with open(path, "r", encoding="utf-8") as source:
                data = json.load(source)
        except PermissionError:
            if attempt + 1 >= _RETRY_ATTEMPTS:
                return None
            time.sleep(_RETRY_DELAY_SECONDS)
            continue
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _write_atomic(path: pathlib.Path, body: str) -> None:
    """Write ``body`` to ``path`` through a fsynced temporary file.

    ``mkstemp`` creates the temporary file with mode 0600 and an exclusive
    name, so no other writer or reader can observe it. The rename replaces the
    previous trace in one step, and is retried while a reader holds the old
    file open on Windows.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX
    )
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _replace_with_retry(source: pathlib.Path, destination: pathlib.Path) -> None:
    """Rename ``source`` over ``destination``, retrying a sharing violation.

    A reader that holds the old trace open makes the rename fail on Windows.
    The reader is short-lived, so a few short retries beat a lost write.
    """

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if attempt >= _RETRY_ATTEMPTS or not _is_sharing_error(error):
                raise
            time.sleep(_RETRY_DELAY_SECONDS)


def _is_sharing_error(error: BaseException) -> bool:
    """Return True when ``error`` looks like a file-sharing violation."""

    if isinstance(error, PermissionError):
        return True
    if getattr(error, "winerror", None) in _SHARING_WINERRORS:
        return True
    return getattr(error, "errno", None) in _SHARING_ERRNOS


def _is_safe_recording_id(recording_id: str) -> bool:
    """Return True when the id cannot escape the storage directory."""

    if not isinstance(recording_id, str) or not recording_id:
        return False
    if recording_id in {".", ".."}:
        return False
    if any(character in recording_id for character in "\x00/\\:"):
        return False
    return True


def _duration(value: Any) -> float | None:
    """Normalise a duration in milliseconds to a rounded float."""

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("duration_ms must be a number or None")
    return round(float(value), 3)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool)) and value is not None


def _log_value(value: Any) -> str:
    """Format one log value on a single line, capped at a short length."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    text = str(value)
    text = " ".join(text.split())
    if len(text) > _LOG_VALUE_CHARS:
        return _truncated(len(text))
    return text


def _log_level(value: str) -> int:
    name = (value or "").strip().upper() or "INFO"
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO
