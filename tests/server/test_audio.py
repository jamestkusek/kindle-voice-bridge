"""Unit tests for the contractual WAV parser in docs/protocol.md section 7.5.

These tests use only standard-library WAV construction and never touch the
network, a device or real audio.  They exercise ``parse_wav`` directly so the
WAV rules can be checked without the HTTP layer.
"""

from __future__ import annotations

import functools
import io
import math
import random
import struct
import wave

import pytest

from voice_bridge_server.audio import WavError, WavInfo, parse_wav

# Shared RIFF/WAVE builders.  Under pytest's default "prepend" mode these
# modules are imported top-level, so the relative import fails and the absolute
# one finds ``_wav_builders`` on the ``tests/server`` entry pytest adds.
try:  # pragma: no cover - branch depends on the pytest import mode
    from . import _wav_builders
except ImportError:  # pragma: no cover
    import _wav_builders

_chunk = _wav_builders.chunk
_fmt_payload = _wav_builders.fmt_payload
_riff = _wav_builders.riff
_extensible_fmt = _wav_builders.extensible_fmt

# 8000 frames is 1 s at 8 kHz, the parser suite's normal body.
_wav = functools.partial(_wav_builders.build_wav, frames=8000)

MAX_DURATION_SECONDS = 15.0


def _stdlib_wav(
    seconds: float = 1.0,
    rate: int = 8000,
    freq: float = 440.0,
) -> bytes:
    """The deterministic sample from docs/protocol.md section 14.1."""
    frames = bytearray()
    for index in range(int(rate * seconds)):
        value = int(0.2 * 32767 * math.sin(2 * math.pi * freq * index / rate))
        frames += struct.pack("<h", value)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))
    return buffer.getvalue()


def _invalid_structural_bodies() -> list[tuple[str, bytes]]:
    fmt = _fmt_payload()
    data = b"\x00" * 16
    valid = _wav(frames=10)
    return [
        ("body-shorter-than-44-bytes", b"RIFF" + struct.pack("<I", 4) + b"WAVE"),
        ("bad-riff-magic", b"XXXX" + valid[4:]),
        ("rifx-big-endian", b"RIFX" + valid[4:]),
        ("rf64", b"RF64" + valid[4:]),
        ("missing-fmt", _riff([_chunk(b"data", data)])),
        ("missing-data", _riff([_chunk(b"fmt ", fmt)])),
        ("data-before-fmt", _riff([_chunk(b"data", data), _chunk(b"fmt ", fmt)])),
        (
            "duplicate-fmt",
            _riff([_chunk(b"fmt ", fmt), _chunk(b"fmt ", fmt), _chunk(b"data", data)]),
        ),
        (
            "duplicate-data",
            _riff([_chunk(b"fmt ", fmt), _chunk(b"data", data), _chunk(b"data", data)]),
        ),
        (
            "fmt-chunk-shorter-than-16",
            _riff([_chunk(b"fmt ", fmt[:14]), _chunk(b"data", b"\x00" * 4)]),
        ),
        ("zero-length-data", _riff([_chunk(b"fmt ", fmt), _chunk(b"data", b"")])),
        (
            "data-size-not-multiple-of-block-align",
            _riff([_chunk(b"fmt ", fmt), _chunk(b"data", b"\x00\x00\x00")]),
        ),
        (
            "block-align-mismatch",
            _riff(
                [
                    _chunk(b"fmt ", _fmt_payload(block_align=4)),
                    _chunk(b"data", b"\x00" * 4),
                ]
            ),
        ),
        (
            "byte-rate-mismatch",
            _riff(
                [
                    _chunk(b"fmt ", _fmt_payload(byte_rate=16001)),
                    _chunk(b"data", data),
                ]
            ),
        ),
        (
            "chunk-overruns-body",
            _riff(
                [
                    _chunk(b"fmt ", fmt),
                    b"data" + struct.pack("<I", 1000) + b"\x00" * 10,
                ]
            ),
        ),
        ("truncated-mid-data", _wav(frames=100)[:-10]),
    ]


INVALID_STRUCTURAL_BODIES = _invalid_structural_bodies()

UNSUPPORTED_FORMAT_BODIES = [
    ("stereo", _wav(channels=2, frames=10)),
    ("24-bit", _wav(bits=24, frames=10)),
    ("32-bit", _wav(bits=32, frames=10)),
    ("4-khz", _wav(rate=4000, frames=10)),
    ("96-khz", _wav(rate=96000, frames=10)),
    ("ieee-float", _wav(audio_format=3, frames=10)),
    (
        "extensible",
        _riff([_chunk(b"fmt ", _extensible_fmt()), _chunk(b"data", b"\x00" * 16)]),
    ),
]


def test_parses_valid_16_bit_mono_wav() -> None:
    body = _wav(frames=8000)

    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert isinstance(info, WavInfo)
    assert info.sample_rate_hz == 8000
    assert info.channels == 1
    assert info.bits_per_sample == 16
    assert info.data_bytes == 16000
    assert info.duration_seconds == 1.0


def test_parses_stdlib_wave_sample() -> None:
    body = _stdlib_wav()

    assert len(body) == 16044
    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.sample_rate_hz == 8000
    assert info.channels == 1
    assert info.bits_per_sample == 16
    assert info.data_bytes == 16000
    assert info.duration_seconds == 1.0


@pytest.mark.parametrize(
    ("rate", "bits"),
    [
        (8000, 8),
        (8000, 16),
        (16000, 16),
        (48000, 8),
        (48000, 16),
    ],
)
def test_accepts_supported_rates_and_depths(rate: int, bits: int) -> None:
    body = _wav(rate=rate, bits=bits, frames=10)

    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.sample_rate_hz == rate
    assert info.channels == 1
    assert info.bits_per_sample == bits
    assert info.data_bytes == 10 * (bits // 8)


@pytest.mark.parametrize("rate", [7999, 4000, 48001, 96000])
def test_rejects_sample_rate_outside_supported_range(rate: int) -> None:
    body = _wav(rate=rate, frames=10)

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "unsupported_wav_format"


@pytest.mark.parametrize("bits", [24, 32])
def test_rejects_unsupported_bit_depth(bits: int) -> None:
    body = _wav(bits=bits, frames=10)

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "unsupported_wav_format"


def test_rejects_stereo() -> None:
    body = _wav(channels=2, frames=10)

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "unsupported_wav_format"


@pytest.mark.parametrize("name,body", UNSUPPORTED_FORMAT_BODIES, ids=[n for n, _ in UNSUPPORTED_FORMAT_BODIES])
def test_rejects_unsupported_formats(name: str, body: bytes) -> None:
    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "unsupported_wav_format"


@pytest.mark.parametrize("name,body", INVALID_STRUCTURAL_BODIES, ids=[n for n, _ in INVALID_STRUCTURAL_BODIES])
def test_rejects_invalid_structure(name: str, body: bytes) -> None:
    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "invalid_wav"


def test_accepts_unknown_chunks_and_trailing_bytes() -> None:
    body = _wav(
        frames=10,
        extra_chunks=(
            _chunk(b"LIST", b"abc"),
            _chunk(b"fact", b"\x00" * 4),
        ),
        trailing=b"\xff" * 7,
    )

    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.data_bytes == 20


def test_accepts_a_legitimate_wav_with_a_consistent_riff_size() -> None:
    body = _wav(frames=10)

    assert struct.unpack("<I", body[4:8])[0] == len(body) - 8
    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.data_bytes == 20


def test_rejects_riff_size_claiming_bytes_beyond_the_body() -> None:
    body = _wav(frames=10)
    overstated = body[:4] + struct.pack("<I", len(body) - 8 + 64) + body[8:]

    with pytest.raises(WavError) as excinfo:
        parse_wav(overstated, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "invalid_wav"


def _riff_size_omitting_required_chunks() -> list[tuple[str, bytes]]:
    fmt_chunk = _chunk(b"fmt ", _fmt_payload())
    data_chunk = _chunk(b"data", b"\x00" * 16)
    return [
        (
            "container-ends-before-fmt",
            _riff([fmt_chunk, data_chunk], riff_size=4),
        ),
        (
            "container-ends-before-data",
            _riff([fmt_chunk, data_chunk], riff_size=4 + len(fmt_chunk)),
        ),
    ]


RIFF_SIZE_OMITTING_REQUIRED_CHUNKS = _riff_size_omitting_required_chunks()


@pytest.mark.parametrize(
    "name,body",
    RIFF_SIZE_OMITTING_REQUIRED_CHUNKS,
    ids=[name for name, _ in RIFF_SIZE_OMITTING_REQUIRED_CHUNKS],
)
def test_rejects_riff_size_that_omits_required_chunks(
    name: str, body: bytes
) -> None:
    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "invalid_wav"


def test_rejects_a_truncated_duplicate_chunk_inside_the_declared_container() -> None:
    fmt_chunk = _chunk(b"fmt ", _fmt_payload())
    data_chunk = _chunk(b"data", b"\x00" * 16)
    truncated_duplicate = b"fmt " + struct.pack("<I", 1000) + b"\x00" * 10
    body = _riff([fmt_chunk, data_chunk, truncated_duplicate])

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "invalid_wav"


def test_accepts_fmt_chunk_larger_than_sixteen_bytes() -> None:
    body = _wav(frames=10, fmt_extra=b"\x00\x00")

    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.sample_rate_hz == 8000
    assert info.bits_per_sample == 16


def test_accepts_odd_sized_data_payload_with_padding() -> None:
    body = _wav(bits=8, frames=3)

    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.bits_per_sample == 8
    assert info.data_bytes == 3


def test_accepts_exactly_fifteen_seconds() -> None:
    body = _wav(rate=8000, bits=16, frames=8000 * 15)

    assert len(body) == 240044
    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.data_bytes == 240000
    assert info.duration_seconds == 15.0


def test_rejects_one_frame_over_fifteen_seconds() -> None:
    body = _wav(rate=8000, bits=16, data=b"\x00" * (8000 * 15 * 2 + 2))

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert excinfo.value.code == "duration_exceeds_limit"


def test_accepts_exactly_zero_point_three_seconds_at_the_configured_limit() -> None:
    body = _wav(rate=8000, bits=16, frames=2400)

    info = parse_wav(body, max_duration_seconds=0.3)

    assert info.data_bytes == 4800
    assert info.duration_seconds == 0.3


def test_rejects_one_frame_over_zero_point_three_seconds() -> None:
    body = _wav(rate=8000, bits=16, frames=2401)

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=0.3)

    assert excinfo.value.code == "duration_exceeds_limit"


def test_respects_a_custom_duration_limit() -> None:
    body = _wav(frames=8000)

    with pytest.raises(WavError) as excinfo:
        parse_wav(body, max_duration_seconds=0.5)

    assert excinfo.value.code == "duration_exceeds_limit"
    assert parse_wav(body, max_duration_seconds=1.0).duration_seconds == 1.0


def test_duration_is_rounded_to_three_decimals() -> None:
    body = _wav(frames=1234)

    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.data_bytes == 2468
    assert info.duration_seconds == 0.154


def test_accepts_forty_eight_khz_fifteen_seconds() -> None:
    body = _wav(rate=48000, bits=16, frames=48000 * 15)

    assert len(body) == 1_440_044
    info = parse_wav(body, max_duration_seconds=MAX_DURATION_SECONDS)

    assert info.sample_rate_hz == 48000
    assert info.duration_seconds == 15.0


def test_error_is_a_value_error_with_code_and_message() -> None:
    with pytest.raises(WavError) as excinfo:
        parse_wav(b"not a wav", max_duration_seconds=MAX_DURATION_SECONDS)

    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.code == "invalid_wav"
    assert isinstance(error.message, str)
    assert error.message


def test_every_prefix_is_handled_without_an_unexpected_exception() -> None:
    body = _wav(rate=8000, bits=8, frames=10)

    for cut in range(len(body) + 1):
        candidate = body[:cut]
        try:
            info = parse_wav(candidate, max_duration_seconds=MAX_DURATION_SECONDS)
        except WavError:
            continue
        assert isinstance(info, WavInfo)


def test_mutated_bodies_never_raise_an_unexpected_exception() -> None:
    body = _wav(rate=8000, bits=8, frames=10)
    rng = random.Random(20261110)

    for _ in range(200):
        mutated = bytearray(body)
        for _ in range(rng.randint(1, 3)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        try:
            parse_wav(bytes(mutated), max_duration_seconds=MAX_DURATION_SECONDS)
        except WavError:
            pass
