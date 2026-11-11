"""Storage boundary for accepted recordings.

The HTTP layer knows only :class:`RecordingStore`. The v1 implementation writes
local files atomically. A temp file in the destination directory is fully
written, flushed and fsynced. The store then renames it onto the final
``<id>.wav`` name. A partially written file is never visible under the final
name, and a failed save leaves no final file behind.

The store does not validate WAV, generate IDs or log. :func:`new_recording_id`
generates IDs. It runs on the server only and never derives an ID from client
input.
"""

from __future__ import annotations

import os
import pathlib
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Protocol

_RECORDING_ID_HEX_BYTES = 16

#: The exact ID format from ``docs/protocol.md`` §9.2. ``fullmatch`` is used so
#: no prefix, suffix, separator or path component can slip through. The review
#: routes and the transcription worker both validate against this one pattern.
RECORDING_ID_PATTERN = re.compile(r"rec_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{32}")


class RecordingStore(Protocol):
    """Minimal persistence seam used by the HTTP layer."""

    def save(self, recording_id: str, body: bytes) -> pathlib.Path:
        """Persist ``body`` under ``recording_id`` and return the final path.

        The call either persists the complete body and returns the final path,
        or raises ``OSError`` and leaves no final file. The write is atomic for
        concurrent readers. A partially written file is never visible under the
        final name.
        """
        ...


class LocalFileStore:
    """Atomic local-filesystem implementation of :class:`RecordingStore`."""

    def __init__(self, storage_dir: str | os.PathLike[str]) -> None:
        self._storage_dir = pathlib.Path(storage_dir)

    @property
    def storage_dir(self) -> pathlib.Path:
        return self._storage_dir

    def save(self, recording_id: str, body: bytes) -> pathlib.Path:
        if not _is_safe_recording_id(recording_id):
            raise OSError("Invalid recording id.")

        final_path = self._storage_dir / f"{recording_id}.wav"
        temp_path: pathlib.Path | None = (
            self._storage_dir / f".incoming-{uuid.uuid4().hex}.part"
        )
        file_descriptor: int | None = None

        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            file_descriptor = os.open(temp_path, flags, 0o600)
            with os.fdopen(file_descriptor, "wb") as handle:
                file_descriptor = None
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())

            if temp_path.stat().st_size != len(body):
                raise OSError("Temporary file size does not match the body.")

            if final_path.exists():
                raise FileExistsError(
                    f"Recording {recording_id} already exists."
                )

            os.replace(temp_path, final_path)
            temp_path = None
            _fsync_directory(self._storage_dir)
            return final_path
        except BaseException:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise


def new_recording_id(now: datetime | None = None) -> str:
    """Return an opaque, filename-safe recording ID.

    Format: ``rec_<UTC yyyymmdd>T<UTC HHMMSS>Z_<32 lowercase hex>``.
    """

    if now is None:
        moment = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        moment = now.replace(tzinfo=timezone.utc)
    else:
        moment = now.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(_RECORDING_ID_HEX_BYTES)
    return f"rec_{stamp}_{suffix}"


def _is_safe_recording_id(recording_id: str) -> bool:
    if not isinstance(recording_id, str) or not recording_id:
        return False
    if recording_id in {".", ".."}:
        return False
    if "\x00" in recording_id:
        return False
    if "/" in recording_id or "\\" in recording_id or ":" in recording_id:
        return False
    return True


def _fsync_directory(directory: pathlib.Path) -> None:
    """Best-effort directory fsync. The function does nothing on Windows."""

    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
