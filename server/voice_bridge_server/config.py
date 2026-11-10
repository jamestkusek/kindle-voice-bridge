"""Immutable receiver configuration.

``Settings`` validates and normalises values only. It does not touch the
filesystem, read the environment or log. Filesystem checks belong to
:func:`voice_bridge_server.app.create_app`.
"""

from __future__ import annotations

import math
import os
import pathlib
from dataclasses import dataclass

_ASCII_WHITESPACE = frozenset(" \t\r\n")


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated receiver settings.

    ``storage_dir`` accepts a ``str`` or ``os.PathLike``. The field is
    normalised to an absolute, expanded :class:`pathlib.Path`.
    """

    token: str
    storage_dir: pathlib.Path
    max_body_bytes: int = 2 * 1024 * 1024
    max_duration_seconds: float = 15.0

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

        raw_storage_dir = self.storage_dir
        if not isinstance(raw_storage_dir, (str, os.PathLike)):
            raise ValueError("storage_dir must be a filesystem path")
        try:
            raw_path = os.fspath(raw_storage_dir)
        except TypeError as exc:  # pragma: no cover - defensive
            raise ValueError("storage_dir must be a filesystem path") from exc
        if isinstance(raw_path, bytes):
            raise ValueError("storage_dir must be a text path, not bytes")
        if not raw_path:
            raise ValueError("storage_dir must not be empty")
        try:
            resolved = pathlib.Path(raw_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("storage_dir could not be resolved") from exc
        object.__setattr__(self, "storage_dir", resolved)

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
