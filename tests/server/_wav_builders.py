"""Shared standard-library WAV builders for the receiver test suites.

``test_app.py`` and ``test_audio.py`` both build RIFF/WAVE bodies by hand, so
the construction logic lives here once.  ``build_wav`` requires ``frames``;
each test module binds its own default with ``functools.partial`` (100 frames
for the HTTP contract tests, 8000 for the parser tests).  Nothing here touches
the network, a device or real audio.

Not named ``test_*.py`` so pytest does not collect it.
"""

from __future__ import annotations

import struct


def chunk(chunk_id: bytes, payload: bytes) -> bytes:
    """Build one RIFF chunk, including the pad byte for odd payloads."""
    if len(chunk_id) != 4:
        raise ValueError("chunk id must be four bytes")
    padding = b"\x00" if len(payload) % 2 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def fmt_payload(
    *,
    rate: int = 8000,
    bits: int = 16,
    channels: int = 1,
    audio_format: int = 1,
    block_align: int | None = None,
    byte_rate: int | None = None,
    extra: bytes = b"",
) -> bytes:
    """Build the payload of a ``fmt `` chunk (16 bytes plus ``extra``)."""
    align = channels * bits // 8 if block_align is None else block_align
    computed_byte_rate = rate * align if byte_rate is None else byte_rate
    return (
        struct.pack(
            "<HHIIHH",
            audio_format,
            channels,
            rate,
            computed_byte_rate,
            align,
            bits,
        )
        + extra
    )


def riff(
    chunks: list[bytes],
    *,
    riff: bytes = b"RIFF",
    wave_form: bytes = b"WAVE",
    riff_size: int | None = None,
) -> bytes:
    """Wrap ``chunks`` in a RIFF container, optionally with a lying size field."""
    payload = wave_form + b"".join(chunks)
    size = len(payload) if riff_size is None else riff_size
    return riff + struct.pack("<I", size) + payload


def build_wav(
    *,
    frames: int,
    rate: int = 8000,
    bits: int = 16,
    channels: int = 1,
    data: bytes | None = None,
    audio_format: int = 1,
    extra_chunks: tuple[bytes, ...] = (),
    fmt_extra: bytes = b"",
    block_align: int | None = None,
    byte_rate: int | None = None,
    trailing: bytes = b"",
) -> bytes:
    """Build a ``fmt `` + ``data`` WAV body; ``frames`` is required.

    Without ``data`` the payload is ``frames`` blocks of silent samples,
    ``frames * block_align`` zero bytes.
    """
    align = channels * bits // 8 if block_align is None else block_align
    if data is None:
        data = b"\x00" * (frames * align)
    fmt = fmt_payload(
        rate=rate,
        bits=bits,
        channels=channels,
        audio_format=audio_format,
        block_align=block_align,
        byte_rate=byte_rate,
        extra=fmt_extra,
    )
    chunks = [chunk(b"fmt ", fmt), *extra_chunks, chunk(b"data", data)]
    return riff(chunks) + trailing


def extensible_fmt() -> bytes:
    """A 40-byte WAVE_FORMAT_EXTENSIBLE fmt chunk whose subformat is PCM."""
    pcm_subformat = (
        b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    )
    extra = (
        struct.pack("<H", 22)
        + struct.pack("<H", 16)
        + struct.pack("<I", 0)
        + pcm_subformat
    )
    return fmt_payload(audio_format=0xFFFE, extra=extra)
