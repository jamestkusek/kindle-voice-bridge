"""Immutable receiver configuration.

``Settings`` validates and normalises values only. It does not touch the
filesystem, read the environment or log. Filesystem checks belong to
:func:`voice_bridge_server.app.create_app`.

The module also holds :func:`token_matches`, the one bearer comparison the HTTP
layer and the debug routes share. It is pure and constant time for equal-length
tokens, and it never logs the presented value.
"""

from __future__ import annotations

import math
import os
import pathlib
import secrets
from dataclasses import dataclass

_ASCII_WHITESPACE = frozenset(" \t\r\n")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated receiver settings.

    ``storage_dir`` accepts a ``str`` or ``os.PathLike``. The field is
    normalised to an absolute, expanded :class:`pathlib.Path`. ``library_dir``
    and ``library_source`` follow the same rule when set and stay ``None``
    otherwise, so a caller that builds ``Settings`` directly keeps its own
    default. ``library_dir`` is the extracted-book store; ``library_source`` is
    the folder the sync scans.

    ``debug_text`` keeps prompts, passages and context text in the trace files.
    ``log_level`` is the level name that :func:`voice_bridge_server.trace.
    configure_logging` applies.
    """

    token: str
    storage_dir: pathlib.Path
    library_dir: pathlib.Path | None = None
    library_source: pathlib.Path | None = None
    max_body_bytes: int = 2 * 1024 * 1024
    max_duration_seconds: float = 15.0
    debug_text: bool = True
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        token = self.token
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if any(character in _ASCII_WHITESPACE for character in token):
            raise ValueError("token must not contain ASCII whitespace")
        # The client sends the bearer token in an HTTP header, so only ASCII is
        # transport-safe. This check keeps every accepted token byte-for-byte
        # comparable with the value the client sent.
        if not token.isascii():
            raise ValueError("token must contain only ASCII characters")
        # An HTTP header value cannot carry control characters. Limit the token
        # to printable ASCII (0x21-0x7E) so the documented contract holds.
        if any(not 0x21 <= ord(character) <= 0x7E for character in token):
            raise ValueError(
                "token must contain only printable ASCII characters (0x21-0x7E)"
            )

        object.__setattr__(
            self, "storage_dir", _normalise_path(self.storage_dir, "storage_dir")
        )

        if self.library_dir is not None:
            object.__setattr__(
                self, "library_dir", _normalise_path(self.library_dir, "library_dir")
            )
        if self.library_source is not None:
            object.__setattr__(
                self,
                "library_source",
                _normalise_path(self.library_source, "library_source"),
            )

        max_body_bytes = self.max_body_bytes
        if isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int):
            raise ValueError("max_body_bytes must be an integer")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be greater than zero")

        max_duration_seconds = self.max_duration_seconds
        if isinstance(max_duration_seconds, bool) or not isinstance(
            max_duration_seconds, (int, float)
        ):
            raise ValueError("max_duration_seconds must be a number")
        duration = float(max_duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(
                "max_duration_seconds must be a positive finite number"
            )
        object.__setattr__(self, "max_duration_seconds", duration)

        if not isinstance(self.debug_text, bool):
            raise ValueError("debug_text must be a boolean")

        log_level = self.log_level
        if not isinstance(log_level, str) or not log_level.strip():
            raise ValueError("log_level must be a non-empty string")
        object.__setattr__(self, "log_level", log_level.strip().upper())


def token_matches(value: str, expected: str) -> bool:
    """Return True when ``value`` is exactly ``Bearer <expected>``.

    The comparison is constant time for equal-length tokens, and the scheme is
    case-insensitive. A malformed header is rejected rather than raised, so the
    caller can answer 401 without a second check.
    """

    parts = value.split(" ")
    if len(parts) != 2:
        return False
    scheme, token = parts
    if scheme.lower() != "bearer" or not token:
        return False
    try:
        return secrets.compare_digest(
            token.encode("utf-8"), expected.encode("utf-8")
        )
    except (TypeError, UnicodeEncodeError):  # pragma: no cover - defensive
        return False


def _normalise_path(value: object, name: str) -> pathlib.Path:
    """Return ``value`` as an absolute, expanded path or raise ``ValueError``."""

    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{name} must be a filesystem path")
    try:
        raw_path = os.fspath(value)
    except TypeError as exc:  # pragma: no cover - defensive
        raise ValueError(f"{name} must be a filesystem path") from exc
    if isinstance(raw_path, bytes):
        raise ValueError(f"{name} must be a text path, not bytes")
    if not raw_path:
        raise ValueError(f"{name} must not be empty")
    try:
        return pathlib.Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{name} could not be resolved") from exc
