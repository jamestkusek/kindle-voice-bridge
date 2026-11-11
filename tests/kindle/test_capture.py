"""Offline tests for the manual capture cue.

The capture module imports Bumble and termios, so these tests stub termios and
skip when Bumble is not installed. They never touch a UART, a controller or a
headset.
"""

import math
import struct
import sys
import types

import pytest

pytest.importorskip("bumble")

try:  # Windows test hosts have no termios.
    import termios  # noqa: F401
except ImportError:  # pragma: no cover - depends on the host
    sys.modules["termios"] = types.ModuleType("termios")

from kindle.voice_bridge import capture_local  # noqa: E402


class Packet:
    """The part of a Bumble SCO packet the capture reads."""

    def __init__(self, data, status=0):
        self.data = data
        self.packet_status = status


def silence(samples=160):
    return Packet(bytes(samples * 2))


def plateau(samples=160, value=3000):
    """The startup DC artifact: a constant value with no AC content."""
    return Packet(struct.pack(f"<{samples}h", *([value] * samples)))


def tone(samples=160, amplitude=800, period=8):
    return Packet(struct.pack(f"<{samples}h", *[
        int(amplitude * math.sin(2 * math.pi * index / period))
        for index in range(samples)
    ]))


def new_capture(max_duration=15.0):
    return capture_local.Capture(settle=False, max_duration=max_duration)


# The level test ------------------------------------------------------------


def test_ac_rms_reads_digital_silence_as_zero():
    assert capture_local.ac_rms(bytes(320)) == 0.0


def test_ac_rms_ignores_a_dc_plateau():
    # The startup artifact is a constant offset, so it has no AC energy.
    assert capture_local.ac_rms(plateau().data) == 0.0


def test_ac_rms_sees_a_live_signal():
    assert capture_local.ac_rms(tone().data) > capture_local.AUDIO_READY_RMS


# The cue -------------------------------------------------------------------


def test_silence_and_the_dc_plateau_do_not_cue():
    capture = new_capture()

    capture.on_packet(silence())
    capture.on_packet(plateau())

    assert capture.cue_offset is None
    assert not capture.audio_event.is_set()
    assert capture.first_packet_at is not None
    assert capture.first_signal_at is None


def test_the_first_signal_cues_before_its_own_packet():
    capture = new_capture()

    capture.on_packet(silence())
    capture.on_packet(plateau())
    before = len(capture.data)
    capture.on_packet(tone())

    # The packet that proved the microphone is live is the first one kept.
    assert capture.cue_offset == before
    assert capture.audio_event.is_set()
    assert capture.first_signal_at is not None
    assert capture.first_speech_at is None  # one 20 ms packet is not speech


def test_the_cue_does_not_move_after_the_first_signal():
    capture = new_capture()

    capture.on_packet(tone())
    first = capture.cue_offset
    capture.on_packet(silence())
    capture.on_packet(tone())

    assert capture.cue_offset == first


def test_force_cue_trims_from_where_it_is_called():
    capture = new_capture()

    capture.on_packet(silence())
    capture.on_packet(silence())
    capture.force_cue()

    assert capture.cue_offset == len(capture.data)
    assert capture.audio_event.is_set()


def test_force_cue_does_not_move_an_existing_cue():
    capture = new_capture()

    capture.on_packet(tone())
    first = capture.cue_offset
    capture.force_cue()

    assert capture.cue_offset == first


# The duration budget -------------------------------------------------------


def test_the_cap_counts_only_audio_after_the_cue():
    capture = new_capture(max_duration=1.0)
    packet_samples = 160
    # Half a second of silence before the cue must not consume the budget.
    for _ in range(25):
        capture.on_packet(silence(packet_samples))
    capture.on_packet(tone(packet_samples))

    assert capture.cue_offset == 25 * packet_samples * 2
    assert not capture.max_duration_reached

    # Feed more than the remaining budget.
    for _ in range(60):
        capture.on_packet(tone(packet_samples))

    assert capture.max_duration_reached
    assert capture.stop.is_set()
    assert capture._stored_after_cue() == capture.max_pcm_bytes


def test_a_capture_with_no_signal_keeps_everything_until_forced():
    capture = new_capture(max_duration=15.0)

    for _ in range(50):
        capture.on_packet(silence())

    assert capture.cue_offset is None
    assert len(capture.data) == 50 * 160 * 2


# The log line --------------------------------------------------------------


def test_arrival_line_reports_each_delay():
    capture = new_capture()
    capture.sco_opened_at = 100.0
    capture.first_packet_at = 100.3
    capture.first_signal_at = 102.1
    capture.first_speech_at = 102.9
    capture.max_ac_rms = 37.2

    line = capture.arrival_line()

    assert "first packet 0.30 s" in line
    assert "first signal 2.10 s" in line
    assert "first speech 2.90 s" in line
    assert "peak 37.2" in line


def test_arrival_line_reports_missing_stages():
    capture = new_capture()
    capture.sco_opened_at = 100.0
    capture.first_packet_at = 100.1

    line = capture.arrival_line()

    assert "first packet 0.10 s" in line
    assert "first signal never" in line
    assert "first speech never" in line


def test_arrival_line_needs_a_sco_time():
    assert new_capture().arrival_line() is None
