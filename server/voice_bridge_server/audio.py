"""Structural WAV validation for the provisional mono-PCM upload contract.

``parse_wav`` is a pure function: it inspects the buffered body, returns a
:class:`WavInfo` on success and raises :class:`WavError` on any failure. It never
writes, logs or mutates state, and it must not leak ``struct.error`` or
``IndexError`` for malformed input.

The accepted format is intentionally narrow (provisional, see
``docs/protocol.md`` §7): little-endian RIFF/WAVE, PCM format tag 1, mono, 8 or
16 bits, 8 kHz-48 kHz. Encoded audio must never be labelled PCM.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from fractions import Fraction

_RIFF = b"RIFF"
_WAVE = b"WAVE"
_FMT = b"fmt "
_DATA = b"data"

_MIN_BODY_BYTES = 44
_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 48000
_SUPPORTED_BITS = frozenset({8, 16})
_PCM_FORMAT_TAG = 1


@dataclass(frozen=True, slots=True)
class WavInfo:
    """Validated properties of an accepted WAV body."""

    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    data_bytes: int
    duration_seconds: float


class WavError(ValueError):
    """A rejected WAV body with a stable, machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_wav(body: bytes, *, max_duration_seconds: float) -> WavInfo:
    """Validate a complete WAV body and return its properties.

    Raises :class:`WavError` with one of the codes in ``docs/protocol.md`` §7.4.
    """

    if isinstance(max_duration_seconds, bool) or not isinstance(
        max_duration_seconds, (int, float)
    ):
        raise ValueError("max_duration_seconds must be a number")
    duration_limit = float(max_duration_seconds)
    if not math.isfinite(duration_limit) or duration_limit <= 0:
        raise ValueError("max_duration_seconds must be a positive finite number")

    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise WavError("invalid_wav", "Request body is not a byte sequence.")
    data = bytes(body)

    if not data:
        raise WavError("empty_body", "Request body is empty.")
    if len(data) < _MIN_BODY_BYTES:
        raise WavError(
            "invalid_wav", "WAV body is shorter than the minimum header size."
        )
    if data[0:4] != _RIFF or data[8:12] != _WAVE:
        raise WavError(
            "invalid_wav", "Body is not a little-endian RIFF/WAVE file."
        )

    total = len(data)
    position = 12
    fmt_payload: bytes | None = None
    data_bytes: int | None = None

    while position < total:
        if total - position < 8:
            # Trailing bytes after the data payload are ignored; anything else
            # is a truncated chunk header.
            if data_bytes is not None:
                break
            raise WavError("invalid_wav", "WAV chunk header is truncated.")

        chunk_id = data[position : position + 4]
        chunk_size = int.from_bytes(data[position + 4 : position + 8], "little")
        payload_start = position + 8
        payload_end = payload_start + chunk_size
        if payload_end > total:
            # Bytes after the data payload are ignored, so a malformed trailing
            # fragment is not structural damage once data has been seen.
            if data_bytes is not None:
                break
            raise WavError(
                "invalid_wav", "WAV chunk extends beyond the end of the body."
            )

        if chunk_id == _FMT:
            if fmt_payload is not None:
                raise WavError(
                    "invalid_wav", "WAV file contains more than one fmt chunk."
                )
            if data_bytes is not None:
                raise WavError(
                    "invalid_wav", "WAV fmt chunk appears after the data chunk."
                )
            fmt_payload = data[payload_start:payload_end]
        elif chunk_id == _DATA:
            if data_bytes is not None:
                raise WavError(
                    "invalid_wav", "WAV file contains more than one data chunk."
                )
            if fmt_payload is None:
                raise WavError(
                    "invalid_wav", "WAV data chunk appears before the fmt chunk."
                )
            data_bytes = chunk_size

        position = payload_end + (chunk_size & 1)
        if position > total:
            # An odd-sized final payload without its pad byte is tolerated at
            # EOF; a pad that overruns anywhere else is structural damage.
            if position == total + 1:
                position = total
            elif data_bytes is not None:
                break
            else:  # pragma: no cover - unreachable with the checks above
                raise WavError(
                    "invalid_wav",
                    "WAV chunk padding extends beyond the end of the body.",
                )

    if fmt_payload is None:
        raise WavError("invalid_wav", "WAV file has no fmt chunk.")
    if data_bytes is None:
        raise WavError("invalid_wav", "WAV file has no data chunk.")
    if len(fmt_payload) < 16:
        raise WavError("invalid_wav", "WAV fmt chunk is too small.")

    (
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = struct.unpack("<HHIIHH", fmt_payload[:16])

    if audio_format != _PCM_FORMAT_TAG:
        raise WavError("unsupported_wav_format", "Only PCM audio is supported.")
    if channels != 1:
        raise WavError("unsupported_wav_format", "Only mono audio is supported.")
    if bits_per_sample not in _SUPPORTED_BITS:
        raise WavError(
            "unsupported_wav_format", "Only 8-bit or 16-bit audio is supported."
        )
    if not (_MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE):
        raise WavError(
            "unsupported_wav_format",
            "Sample rate must be between 8000 and 48000 Hz.",
        )

    expected_block_align = channels * bits_per_sample // 8
    if block_align != expected_block_align:
        raise WavError(
            "invalid_wav", "WAV block_align is inconsistent with the format."
        )
    expected_byte_rate = sample_rate * block_align
    if byte_rate != expected_byte_rate:
        raise WavError(
            "invalid_wav", "WAV byte_rate is inconsistent with the format."
        )

    if data_bytes <= 0:
        raise WavError("invalid_wav", "WAV data chunk is empty.")
    if data_bytes % block_align != 0:
        raise WavError(
            "invalid_wav",
            "WAV data chunk size is not a multiple of block_align.",
        )

    bytes_per_second = sample_rate * block_align
    # Exact rational comparison avoids float drift at the boundary: exactly
    # max_duration_seconds is accepted, one frame more is rejected.
    if Fraction(data_bytes, bytes_per_second) > Fraction(duration_limit):
        raise WavError(
            "duration_exceeds_limit",
            "Recording duration exceeds the configured limit.",
        )

    duration_seconds = round(data_bytes / bytes_per_second, 3)
    return WavInfo(
        sample_rate_hz=sample_rate,
        channels=channels,
        bits_per_sample=bits_per_sample,
        data_bytes=data_bytes,
        duration_seconds=duration_seconds,
    )
