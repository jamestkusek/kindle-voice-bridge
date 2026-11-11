"""Offline tests for the manual capture cue.

The capture module imports Bumble and termios, so these tests stub termios and
skip when Bumble is not installed. They never touch a UART, a controller or a
headset.

The artifact shape here is taken from a real PW4 recording: the SCO receive path
delivers about 2.4 s of a DC plateau near 21,000 counts with a little AC ripple
before any live audio. AC energy alone cannot reject that, which is why the cue
also tests the DC level.
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

PACKET_SAMPLES = 160  # 20 ms at 8 kHz
WINDOW_PACKETS = int(
    capture_local.AUDIO_READY_WINDOW_SECONDS * capture_local.SAMPLE_RATE
    / PACKET_SAMPLES
)


class Packet:
    """The part of a Bumble SCO packet the capture reads."""

    def __init__(self, data, status=0):
        self.data = data
        self.packet_status = status


def silence(samples=PACKET_SAMPLES):
    return Packet(bytes(samples * 2))


def plateau(samples=PACKET_SAMPLES, value=3000):
    """A constant value with no AC content at all."""
    return Packet(struct.pack(f"<{samples}h", *([value] * samples)))


def artifact(samples=PACKET_SAMPLES, dc=21000, ripple=800):
    """The real startup artifact: a large DC plateau with AC ripple."""
    return Packet(struct.pack(f"<{samples}h", *[
        dc + int(ripple * math.sin(2 * math.pi * index / 16))
        for index in range(samples)
    ]))


def tone(samples=PACKET_SAMPLES, amplitude=800, period=8):
    return Packet(struct.pack(f"<{samples}h", *[
        int(amplitude * math.sin(2 * math.pi * index / period))
        for index in range(samples)
    ]))


def new_capture(max_duration=15.0):
    return capture_local.Capture(settle=False, max_duration=max_duration)


def feed(capture, packet, times=1):
    for _ in range(times):
        capture.on_packet(packet)


# The level test ------------------------------------------------------------


def test_ac_rms_reads_digital_silence_as_zero():
    assert capture_local.ac_rms(bytes(320)) == 0.0


def test_ac_rms_ignores_a_constant_plateau():
    assert capture_local.ac_rms(plateau().data) == 0.0


def test_ac_rms_sees_the_artifact_ripple():
    # This is the reason AC energy alone cannot decide the cue.
    assert capture_local.ac_rms(artifact().data) > capture_local.AUDIO_READY_RMS


# The cue -------------------------------------------------------------------


def test_silence_does_not_cue():
    capture = new_capture()

    feed(capture, silence(), WINDOW_PACKETS * 3)

    assert capture.cue_offset is None
    assert not capture.audio_event.is_set()
    assert capture.first_packet_at is not None
    assert capture.first_signal_at is None


def test_a_constant_plateau_does_not_cue():
    capture = new_capture()

    feed(capture, plateau(), WINDOW_PACKETS * 3)

    assert capture.cue_offset is None
    assert not capture.audio_event.is_set()


def test_the_startup_artifact_does_not_cue():
    capture = new_capture()

    # 2.4 s of the real artifact, the length seen on the device.
    feed(capture, artifact(), 120)

    assert capture.cue_offset is None
    assert not capture.audio_event.is_set()
    assert capture.max_dc > 20000


def test_live_audio_after_the_artifact_cues_at_the_window_start():
    capture = new_capture()
    feed(capture, artifact(), 120)
    artifact_bytes = len(capture.data)

    feed(capture, tone(), WINDOW_PACKETS)

    assert capture.cue_offset == artifact_bytes
    assert capture.audio_event.is_set()
    assert capture.first_signal_at is not None


def test_the_cue_does_not_move_after_it_fires():
    capture = new_capture()

    feed(capture, tone(), WINDOW_PACKETS)
    first = capture.cue_offset
    feed(capture, tone(), 5)
    feed(capture, silence(), 5)

    assert capture.cue_offset == first


def test_a_short_artifact_cannot_decide_on_one_packet():
    capture = new_capture()

    # One clean packet inside the artifact window must not cue.
    feed(capture, artifact(), WINDOW_PACKETS - 1)
    feed(capture, tone(), 1)

    assert capture.cue_offset is None


def test_force_cue_trims_from_where_it_is_called():
    capture = new_capture()

    feed(capture, silence(), 5)
    capture.force_cue()

    assert capture.cue_offset == len(capture.data)
    assert capture.audio_event.is_set()


def test_force_cue_does_not_move_an_existing_cue():
    capture = new_capture()

    feed(capture, tone(), WINDOW_PACKETS)
    first = capture.cue_offset
    capture.force_cue()

    assert capture.cue_offset == first


# The duration budget -------------------------------------------------------


def test_the_cap_counts_only_audio_after_the_cue():
    capture = new_capture(max_duration=1.0)
    # Two seconds of artifact before the cue must not consume the budget.
    feed(capture, artifact(), 100)
    feed(capture, tone(), WINDOW_PACKETS)
    cue = capture.cue_offset
    assert cue > 0
    assert not capture.max_duration_reached

    # Feed more than the remaining budget.
    feed(capture, tone(), 400)

    assert capture.max_duration_reached
    assert capture.stop.is_set()
    assert capture._stored_after_cue() == capture.max_pcm_bytes


def test_a_capture_with_no_signal_keeps_everything_until_forced():
    capture = new_capture(max_duration=15.0)

    feed(capture, silence(), 50)

    assert capture.cue_offset is None
    assert len(capture.data) == 50 * PACKET_SAMPLES * 2


# The log line --------------------------------------------------------------


def test_arrival_line_reports_each_delay():
    capture = new_capture()
    capture.sco_opened_at = 100.0
    capture.first_packet_at = 100.3
    capture.first_signal_at = 102.6
    capture.first_speech_at = 102.9
    capture.max_dc = 21307.0
    capture.max_ac_rms = 15001.0

    line = capture.arrival_line()

    assert "first packet 0.30 s" in line
    assert "mic live 2.60 s" in line
    assert "peak DC 21307" in line
    assert "peak AC RMS 15001" in line
    assert "first speech 2.90 s" in line


def test_arrival_line_reports_missing_stages():
    capture = new_capture()
    capture.sco_opened_at = 100.0
    capture.first_packet_at = 100.1

    line = capture.arrival_line()

    assert "first packet 0.10 s" in line
    assert "mic live never" in line
    assert "first speech never" in line


def test_arrival_line_needs_a_sco_time():
    assert new_capture().arrival_line() is None
