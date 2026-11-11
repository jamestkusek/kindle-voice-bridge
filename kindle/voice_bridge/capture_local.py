#!/usr/bin/env python3
"""Record one headset clip on the Kindle and send it to the receiver.

This script is manual-start only. It performs the PW4 Bluetooth handoff, runs a
Bumble HFP Audio Gateway over the real Bluetooth UART, records mono 16-bit PCM
at 8 kHz, restores stock Bluetooth in a finally path, then uploads the WAV and
deletes it.

The link key is read from the Kindle pairing store at runtime and held in
memory only. The script never writes the key to a file, never prints it, and
never sends it over the network.

The port owner changes during a run:

  * Before the handoff, bsa_server owns /dev/ttymxc2.
  * During the handoff, pw4_supervisor opens the port, records the current
    settings, applies the raw H:4 settings, and closes it again.
  * During the capture, Bumble owns the port through its serial: transport.
    Bumble sets 2,000,000 baud, 8N1, raw input, and RTS/CTS itself.
  * In cleanup, pw4_supervisor reopens the port, restores the settings it
    recorded before the handoff, and restarts btd.

The capture code is a reduced copy of tools/hfp_capture.py. That tool is a
laptop listener and is not deployed to the device. Keep the settle thresholds
in step with it.

--send-silence sends paced silent PCM downlink packets during the capture. Some
handsets start their microphone only after downlink audio arrives. The option is
off by default, so the capture path is unchanged unless the user asks for it.

The run is bounded three ways: a wall-clock alarm, a capture deadline, and a
maximum usable audio length. A stream that never looks live is a failure, not a
recording.
"""

import argparse
import asyncio
import json
import math
import os
import re
import signal
import struct
import sys
import tempfile
import time
import wave
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass

if __package__:
    from . import pw4_supervisor, uploader
else:
    import pw4_supervisor
    import uploader

from bumble import core, hci, hfp, rfcomm, transport
from bumble.device import Device, DeviceConfiguration
from bumble.keys import KeyStore, PairingKeys


# Bumble's serial: transport opens the port with pyserial. Confirmed from the
# installed pyserial 3.5 source, it sets:
#   * O_RDWR | O_NOCTTY | O_NONBLOCK, then leaves the descriptor non-blocking
#   * 2,000,000 baud, CS8, no parity, one stop bit, CLOCAL | CREAD
#   * CRTSCTS because of the rtscts token
#   * VMIN 0 and VTIME 0, the same as pw4_supervisor.configure_uart
#   * iflag cleared, which includes IGNBRK. pw4_supervisor sets IGNBRK, so
#     Bumble's settings win and a break arrives as a zero byte
#   * a flush of the input buffer, then DTR asserted
# The supervisor's own descriptor is closed before Bumble opens the port, so
# Bumble is the only holder during the capture. On cleanup the supervisor
# reopens the port and restores the settings it recorded before the handoff.
SERIAL_SPEC = "serial:/dev/ttymxc2,2000000,rtscts"
PAIRING_STORE = "/var/local/zbluetooth/bt_devices.xml"

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
CHANNELS = 1
DURATION = 5.0

# Settle detector. The three real captures in .data/ show three signatures,
# measured per 0.40 s window of 60-byte packets:
#   DC artifact    max |packet mean| 14,959..21,346; ZCR 0..148/s; RMS 70..1,037
#   31,898 plateau max |packet mean| 31,898..32,721; ZCR 0/s; RMS 0..1,037
#   live speech    max |packet mean| 113..641;      ZCR 1,057..2,921/s; RMS 161..943
# The thresholds sit between the two groups. The detector assumes PCM16 mono at
# 8 kHz. If the controller returns encoded bytes its verdict is meaningless.
SETTLE_WINDOW_SECONDS = 0.40
SETTLE_MEAN_LIMIT = 2000.0
SETTLE_ZCR_MIN = 600.0
SETTLE_RMS_MIN = 60.0
MAX_SETTLE_SECONDS = 10.0

# The manual path cues the user when the microphone starts converting audio,
# which is not the same moment as SCO open. A live microphone has an AC noise
# floor; digital silence and the startup DC plateau have none. This threshold is
# far below SETTLE_RMS_MIN because it decides "converting", not "speech".
AUDIO_READY_RMS = 4.0
# The startup artifact is a DC plateau around 21,000 counts with a little AC
# ripple, so AC energy alone cannot reject it. The cue needs the DC test as
# well, over a short window so one odd packet cannot decide.
AUDIO_READY_WINDOW_SECONDS = 0.20
# If no signal appears, start anyway so a quiet headset cannot stall the run.
AUDIO_READY_TIMEOUT = 4.0

MAX_PCM_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH * (DURATION + 2.0))

# The alarm stops the run. The deadline leaves time for the Bluetooth restore
# after the alarm, so cleanup never starts with no time left.
TOTAL_SECONDS = 120
CLEANUP_RESERVE_SECONDS = 30
CAPTURE_DEADLINE_SECONDS = 90.0

PORT_TIMEOUT = 20.0
CONTROLLER_TIMEOUT = 30.0
CONNECT_TIMEOUT = 20.0
AUTH_TIMEOUT = 20.0
ENCRYPT_TIMEOUT = 10.0
SDP_TIMEOUT = 20.0
RFCOMM_TIMEOUT = 15.0
SLC_TIMEOUT = 20.0
CALL_TIMEOUT = 5.0
SCO_TIMEOUT = 20.0
CLEANUP_TIMEOUT = 3.0
UPLOAD_TIMEOUT = 30
CALL_INDICATOR_PAUSE = 0.5

AUTHENTICATED_KEY_TYPES = (0x05, 0x08)
HEX = "0123456789abcdef"

WARNINGS = (
    "This script is manual-start only.",
    "A crash can leave Bluetooth unusable. D: access may disappear.",
    "Reboot is not guaranteed to restore Bluetooth.",
    "Cleanup does not prove that stock Bluetooth works.",
    "The watchdog cannot protect against SIGKILL or a hung kernel.",
    "The PID check and signal are not atomic. PID reuse can signal another process.",
    "The link key stays in process memory. The script never writes it to a file.",
    "The upload can use plain HTTP. The token and the audio are then visible on the network.",
)

PHASE_NAMES = {
    "port": "serial port",
    "controller": "controller setup",
    "connect": "headset connection",
    "authenticate": "authentication",
    "encrypt": "encryption",
    "sdp": "service search",
    "rfcomm": "RFCOMM setup",
    "slc": "HFP setup",
    "call": "call simulation",
    "sco": "audio link setup",
    "capture": "capture",
}


class SetupError(Exception):
    """A problem before any device state changes."""


class CaptureFailure(Exception):
    """A known capture failure with a short, safe message."""


# -----------------------------------------------------------------------------
# Configuration and pairing store
# -----------------------------------------------------------------------------
def read_config(path):
    """Read the upload configuration and leave the file in place.

    The file is the user's configuration, so reading it must not destroy it.
    Later runs reuse the same file. The token stays on the device in plain
    text, which is acceptable only for the trusted-LAN prototype.
    """
    try:
        with open(path, "r", encoding="utf-8") as source:
            text = source.read()
    except FileNotFoundError as error:
        raise SetupError(
            f"upload.json is missing at {path}. Write "
            '{"server_url": ..., "token": ...} there and start the session again.'
        ) from error
    except OSError as error:
        raise SetupError(f"The configuration file at {path} cannot be read.") from error
    try:
        config = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise SetupError(
            f"The configuration file at {path} is not valid JSON.") from error
    if (not isinstance(config, dict) or
            not isinstance(config.get("server_url"), str) or not config["server_url"] or
            not isinstance(config.get("token"), str) or not config["token"]):
        raise SetupError(
            f"The configuration at {path} needs non-empty server_url and token strings.")
    return config


def hex_digits(text, digits):
    """Return the hex digits of a colon-separated value, or None."""
    value = "".join(text.split()).replace(":", "").lower()
    if len(value) != digits or any(char not in HEX for char in value):
        return None
    return value


def read_paired_devices(path):
    """Return [(address, key_bytes, key_type)] from the Kindle pairing store.

    The PW4 shape is <bt_devices> holding <device> elements with <bd_addr>,
    <link_key> and <key_type> children. Values are colon-separated hex with a
    trailing colon.
    """
    devices = []
    for element in ET.parse(path).getroot().iter("device"):
        fields = {child.tag.strip().lower(): (child.text or "") for child in element}
        address = hex_digits(fields.get("bd_addr", ""), 12)
        key = hex_digits(fields.get("link_key", ""), 32)
        if address is None or key is None:
            continue
        key_type = fields.get("key_type", "").strip()
        devices.append((
            ":".join(address[index:index + 2] for index in range(0, 12, 2)).upper(),
            bytes.fromhex(key),
            int(key_type) if key_type.isdigit() else None,
        ))
    return devices


class MemoryKeys(KeyStore):
    """An in-memory Bumble key store. It holds one classic link key."""

    def __init__(self, entries):
        self.entries = entries

    async def get(self, name):
        return self.entries.get(name)


def load_keystore(path, wanted=None):
    """Build an in-memory key store for one paired device.

    Returns (keystore, address). The key is byte-reversed because this headset
    authenticates only in that order. The stored order fails with
    AUTHENTICATION_FAILURE_ERROR.
    """
    try:
        devices = read_paired_devices(path)
    except OSError as error:
        raise SetupError(f"The pairing store {path} cannot be read.") from error
    except ET.ParseError as error:
        raise SetupError(f"The pairing store {path} is not valid XML.") from error
    if wanted is not None:
        chosen = [device for device in devices if device[0] == wanted]
        if not chosen:
            raise SetupError(f"The pairing store has no usable entry for {wanted}.")
    elif len(devices) == 1:
        chosen = devices
    elif not devices:
        raise SetupError("The pairing store has no entry with a usable link key.")
    else:
        addresses = ", ".join(sorted(device[0] for device in devices))
        raise SetupError(
            "The pairing store holds more than one device. "
            f"Give --headset with one of: {addresses}.")
    address, key, key_type = chosen[0]
    entry = PairingKeys(
        link_key=PairingKeys.Key(
            value=bytes(reversed(key)),
            authenticated=key_type in AUTHENTICATED_KEY_TYPES,
        ),
        link_key_type=key_type,
    )
    entries = {address: entry, address + "/P": entry}
    return MemoryKeys(entries), address


# -----------------------------------------------------------------------------
# HFP capture
# -----------------------------------------------------------------------------
def ac_rms(payload):
    """Return the AC RMS of one PCM16 payload, in sample counts.

    The DC component is removed first, so a constant plateau reads as zero. This
    is what separates a live microphone from digital silence and from the
    startup DC artifact.
    """

    count = len(payload) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", payload[:count * 2])
    mean = sum(samples) / count
    return math.sqrt(sum((value - mean) ** 2 for value in samples) / count)


class SettleDetector:
    """Decide when the SCO receive path starts delivering live audio.

    A sliding window over the packet callback rejects the startup artifact and
    accepts the first window that looks like live audio. The window is trimmed
    to at least SETTLE_WINDOW_SECONDS of samples, so the decision lags the
    window start by one window. settle_seconds names the window start, which is
    the first sample worth keeping.
    """

    def __init__(self, window_seconds=SETTLE_WINDOW_SECONDS,
                 mean_limit=SETTLE_MEAN_LIMIT, zcr_min=SETTLE_ZCR_MIN,
                 rms_min=SETTLE_RMS_MIN, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.mean_limit = mean_limit
        self.zcr_min = zcr_min
        self.rms_min = rms_min
        self.window_samples = max(1, int(round(window_seconds * sample_rate)))
        self.samples_seen = 0
        self.settled = False
        self.settle_seconds = None
        self.criterion = None
        self._window = deque()
        self._window_samples = 0
        self._previous_sample = None

    def feed(self, payload):
        """Add one SCO payload. Return True on the packet that settles."""
        if self.settled:
            return False
        count = len(payload) // 2
        if count == 0:
            return False
        samples = struct.unpack(f"<{count}h", payload[:count * 2])
        mean = sum(samples) / count
        energy = sum((value - mean) ** 2 for value in samples)
        crossings = 0
        previous = self._previous_sample if self._previous_sample is not None else samples[0]
        for value in samples:
            if (previous < 0 <= value) or (previous >= 0 > value):
                crossings += 1
            previous = value
        self._previous_sample = samples[-1]
        self.samples_seen += count

        self._window.append((count, mean, energy, crossings))
        self._window_samples += count
        while self._window and self._window_samples - self._window[0][0] >= self.window_samples:
            self._window_samples -= self._window.popleft()[0]
        if self._window_samples < self.window_samples:
            return False

        window = self._window_samples
        # A single plateau packet inside the window must fail the DC test, so
        # take the largest packet mean rather than the mean of the means.
        dc = max(abs(entry[1]) for entry in self._window)
        rms = math.sqrt(sum(entry[2] for entry in self._window) / window)
        zcr = sum(entry[3] for entry in self._window) * self.sample_rate / window
        if dc <= self.mean_limit and zcr >= self.zcr_min and rms >= self.rms_min:
            self.settled = True
            self.settle_seconds = (self.samples_seen - window) / self.sample_rate
            self.criterion = {
                "max_abs_packet_dc": round(dc, 1),
                "zero_crossing_rate_per_second": round(zcr, 1),
                "rms": round(rms, 1),
            }
            return True
        return False


@dataclass(frozen=True)
class CaptureReport:
    """The result of one capture, with the measurements the log needs."""

    pcm: bytes
    settle_seconds: float | None
    settle_after_sco_seconds: float | None
    silence_used: bool
    silence_error: str | None


class Capture:
    """Collect SCO PCM, optionally applying the one-shot settle detector.

    The resident manual-recording path uses ``settle=False``. It stores the
    first valid packet immediately and bounds the buffer to ``max_duration``.
    """

    def __init__(self, settle=True, max_duration=DURATION):
        if max_duration <= 0:
            raise ValueError("max_duration must be greater than zero")
        self.data = bytearray()
        self.stop = asyncio.Event()
        self.disconnect = False
        self.invalid_status = False
        self.detector = SettleDetector() if settle else None
        self.max_duration = float(max_duration)
        self.max_samples = int(round(self.max_duration * SAMPLE_RATE))
        self.max_pcm_bytes = (MAX_PCM_BYTES if settle else
                              self.max_samples * SAMPLE_WIDTH)
        self.max_duration_reached = False
        self.settled = False
        self.settle_seconds = None
        self.settle_criterion = None
        self.discarded_packets = 0
        self.samples = 0
        # The daemon waits on this event to report the settled phase.
        self.settled_event = asyncio.Event()
        self.sco_opened_at = None
        self.settled_at = None
        self.settle_after_sco_seconds = None
        self.silence_used = False
        self.silence_error = None
        # Manual path: when the microphone actually started converting audio.
        # Everything before cue_offset is trimmed from the WAV, so the user gets
        # the full max_duration from the moment they are told to speak.
        self.cue_offset = None
        self.cue_at = None
        self.audio_event = asyncio.Event()
        self.first_packet_at = None
        self.first_signal_at = None
        self.first_speech_at = None
        self.max_ac_rms = 0.0
        self.max_dc = 0.0
        # Before the cue there is no duration budget, so bound the buffer by
        # the readiness timeout instead and cue when it is reached.
        self.pre_cue_limit = int(
            (AUDIO_READY_TIMEOUT + 1.0) * SAMPLE_RATE * SAMPLE_WIDTH)
        # Decides the cue: no DC plateau and some AC energy. This is the settle
        # detector with a much lower RMS floor and no zero-crossing rule,
        # because it decides "converting", not "speech".
        self.ready = SettleDetector(
            window_seconds=AUDIO_READY_WINDOW_SECONDS,
            mean_limit=SETTLE_MEAN_LIMIT,
            zcr_min=0.0,
            rms_min=AUDIO_READY_RMS,
        )
        # Measurement only. It never decides anything on the manual path.
        self.probe = SettleDetector()
        # Hold only the tail that a settle decision can still reach back to.
        self.pending = deque()
        self.pending_samples = 0
        self.pending_start = 0
        self.pending_limit = int((SETTLE_WINDOW_SECONDS + 0.5) * SAMPLE_RATE)

    def on_packet(self, packet):
        if self.stop.is_set():
            return
        status = int(packet.packet_status)
        if not 0 <= status <= 3:
            self.invalid_status = True
            self.stop.set()
            return
        payload = packet.data
        if self.detector is None:
            self._note_arrival(payload)
            self._store(payload)
            if self.cue_offset is None:
                if len(self.data) >= self.pre_cue_limit:
                    self.force_cue()
                return
            if self._stored_after_cue() >= self.max_pcm_bytes:
                self.max_duration_reached = True
                self.stop.set()
            return
        if not self.settled:
            self._hold(payload)
            if self.detector.feed(payload):
                self._release()
            elif self.detector.samples_seen >= MAX_SETTLE_SECONDS * SAMPLE_RATE:
                self.stop.set()
            return
        self._store(payload)
        if self.samples >= DURATION * SAMPLE_RATE:
            self.stop.set()

    def _note_arrival(self, payload):
        """Timestamp the first packet, the first signal, and the first speech.

        The manual path stores every packet, so these values only drive the cue
        and the log. They never change what is kept.
        """
        now = time.monotonic()
        if self.first_packet_at is None:
            self.first_packet_at = now
        level = ac_rms(payload)
        if level > self.max_ac_rms:
            self.max_ac_rms = level
        count = len(payload) // 2
        if count:
            samples = struct.unpack(f"<{count}h", payload[:count * 2])
            dc = abs(sum(samples) / count)
            if dc > self.max_dc:
                self.max_dc = dc
        if self.cue_offset is None and self.ready.feed(payload):
            # Keep from the start of the window that passed, so the cue has a
            # short pre-roll and no part of the accepted audio is trimmed.
            start = int(round(self.ready.settle_seconds * SAMPLE_RATE))
            self.cue_offset = min(start * SAMPLE_WIDTH, len(self.data))
            self.cue_at = now
            self.first_signal_at = now
            self.audio_event.set()
        if self.first_speech_at is None and self.probe.feed(payload):
            self.first_speech_at = now

    def _stored_after_cue(self):
        """Return the bytes that count against the duration budget.

        The settle path has no cue, so every byte counts. The manual path counts
        only bytes after the cue: there is no budget until the user is told to
        speak, and the pre-cue buffer has its own bound.
        """
        if self.cue_offset is not None:
            return len(self.data) - self.cue_offset
        if self.detector is None:
            return 0
        return len(self.data)

    def force_cue(self):
        """Cue without a signal, for a headset that never reports a noise floor."""
        if self.cue_offset is not None:
            return
        self.cue_offset = len(self.data)
        self.cue_at = time.monotonic()
        self.audio_event.set()

    def arrival_line(self):
        """Describe when the audio path came up, relative to SCO open."""
        opened = self.sco_opened_at
        if opened is None:
            return None

        def at(value):
            return "never" if value is None else f"{value - opened:.2f} s"

        return (
            f"Audio: first packet {at(self.first_packet_at)}, "
            f"mic live {at(self.first_signal_at)} "
            f"(window {AUDIO_READY_WINDOW_SECONDS:g} s, "
            f"peak DC {self.max_dc:.0f}, peak AC RMS {self.max_ac_rms:.0f}), "
            f"first speech {at(self.first_speech_at)} after SCO open."
        )

    def _hold(self, payload):
        self.pending.append((self.pending_start, payload))
        self.pending_start += len(payload) // 2
        self.pending_samples += len(payload) // 2
        while (self.pending and
               self.pending_samples - len(self.pending[0][1]) // 2 >= self.pending_limit):
            old = self.pending.popleft()
            self.pending_samples -= len(old[1]) // 2
            self.discarded_packets += 1

    def _release(self):
        self.settled = True
        keep_from = int(round(self.detector.settle_seconds * SAMPLE_RATE))
        self.settle_seconds = round(self.detector.settle_seconds, 3)
        self.settle_criterion = self.detector.criterion
        held = list(self.pending)
        self.pending.clear()
        self.pending_samples = 0
        for start, payload in held:
            if start >= keep_from:
                self._store(payload)
            else:
                self.discarded_packets += 1
        self.settled_at = time.monotonic()
        if self.sco_opened_at is not None:
            self.settle_after_sco_seconds = round(
                self.settled_at - self.sco_opened_at, 3)
        self.settled_event.set()

    def _store(self, payload):
        # Keep whole 16-bit samples, so the WAV data chunk stays even.
        payload = payload[:len(payload) & ~1]
        if not payload:
            return
        remaining = self.max_pcm_bytes - self._stored_after_cue()
        if remaining <= 0:
            if self.detector is None:
                self.max_duration_reached = True
            self.stop.set()
            return
        if len(payload) > remaining:
            if self.detector is not None:
                self.stop.set()
                return
            payload = payload[:remaining & ~1]
            self.max_duration_reached = True
            self.stop.set()
        if not payload:
            return
        self.data.extend(payload)
        self.samples += len(payload) // 2

    def on_disconnect(self, *_args):
        self.disconnect = True
        self.stop.set()


class CaptureAg(hfp.AgProtocol):
    def __init__(self, dlc):
        # The DLC can deliver queued AT commands during the parent constructor.
        self.slc_ready = asyncio.Event()
        self.cmer_received = asyncio.Event()
        states = hfp.AgIndicatorState
        super().__init__(dlc, hfp.AgConfiguration(
            supported_ag_features=[],
            supported_ag_indicators=[states.service(), states.call(), states.callsetup(),
                                     states.callheld(), states.signal(), states.roam(),
                                     states.battchg()],
            supported_hf_indicators=[],
            supported_ag_call_hold_operations=[],
            supported_audio_codecs=[hfp.AudioCodec.CVSD],
        ))

    def _read_at(self, data):
        if len(self.read_buffer) + len(data) > 8192:
            raise CaptureFailure("The HFP command buffer limit was reached.")
        super()._read_at(data)

    def _check_remained_slc_commands(self):
        super()._check_remained_slc_commands()
        if not self._remained_slc_setup_features:
            self.slc_ready.set()

    def _on_cmer(self, *args):
        super()._on_cmer(*args)
        self.cmer_received.set()


class NoPairingDevice(Device):
    """Reject new pairing requests before Bumble's default pairing handlers run."""

    def _reject(self, command):
        async def send():
            try:
                await asyncio.wait_for(self.send_sync_command(command), CLEANUP_TIMEOUT)
            except Exception:
                pass
        asyncio.create_task(send())

    def on_authentication_io_capability_request(self, address):
        self._reject(hci.HCI_IO_Capability_Request_Negative_Reply_Command(
            bd_addr=address, reason=hci.HCI_PAIRING_NOT_ALLOWED_ERROR))

    def on_authentication_user_confirmation_request(self, address, _code):
        self._reject(hci.HCI_User_Confirmation_Request_Negative_Reply_Command(bd_addr=address))

    def on_authentication_user_passkey_request(self, address):
        self._reject(hci.HCI_User_Passkey_Request_Negative_Reply_Command(bd_addr=address))

    def on_pin_code_request(self, address):
        self._reject(hci.HCI_PIN_Code_Request_Negative_Reply_Command(bd_addr=address))


def remaining(deadline, cap):
    left = deadline - time.monotonic()
    if left <= 0:
        raise CaptureFailure("The time limit for the capture expired.")
    return min(cap, left)


async def step(awaitable, deadline, cap, phase):
    """Await one capture step with a time limit."""
    try:
        return await asyncio.wait_for(awaitable, remaining(deadline, cap))
    except TimeoutError:
        raise CaptureFailure(f"The {PHASE_NAMES[phase]} step timed out.") from None


async def quiet(operation):
    try:
        await asyncio.wait_for(operation(), CLEANUP_TIMEOUT)
    except Exception:
        pass


async def simulate_call(ag):
    """Report an incoming call, then an active call, so the HF may open its mic."""
    ag.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP, int(hfp.CallSetupAgIndicator.INCOMING_CALL_PROCESS))
    await asyncio.sleep(CALL_INDICATOR_PAUSE)
    ag.update_ag_indicator(hfp.AgIndicator.CALL, 1)
    ag.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP, int(hfp.CallSetupAgIndicator.NOT_IN_CALL_SETUP))
    await asyncio.sleep(CALL_INDICATOR_PAUSE)


async def send_silence(device, link, capture):
    """Send paced silent PCM downlink packets until the capture stops.

    Some handsets may start their microphone only after downlink audio arrives.
    This mirrors tools/hfp_capture.py --send-pcm-silence. The packet length and
    the pacing use the same 16-bit mono 8 kHz assumption as the settle detector.
    The loop stops on capture.stop or a link error, so the capture end always
    stops the stream.
    """
    size = min(link.tx_packet_length, 240) & ~1
    if size < 2:
        capture.silence_error = (
            "The audio link reported an unusable downlink packet length.")
        return
    interval = size / (SAMPLE_RATE * SAMPLE_WIDTH)
    silence = bytes(size)
    while not capture.stop.is_set():
        try:
            device.host.send_sco_sdu(link.handle, silence)
        except Exception:
            capture.silence_error = (
                "The downlink silence stream stopped on a link error.")
            return
        await asyncio.sleep(interval)


async def setup_sco(device, acl, capture, links):
    """Open the eSCO voice link and attach the capture sink."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def on_sco(new_link):
        if new_link.acl_connection is not acl:
            return
        links.append(new_link)
        if not capture.stop.is_set():
            new_link.sink = capture.on_packet
            new_link.on("disconnection", capture.on_disconnect)
        if not result.done():
            result.set_result(new_link)

    def on_failure(_status):
        if not result.done():
            result.set_exception(CaptureFailure("The audio link failed to open."))

    device.on("sco_connection", on_sco)
    acl.on("sco_connection_failure", on_failure)
    params = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S1]
    command = hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
        connection_handle=acl.handle, **params.asdict())
    await device.send_async_command(command, check_status=True)
    return await result


async def wait_for_uart_release(seconds=3.0):
    """Wait until no process holds the UART, so cleanup can reopen it."""
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        try:
            if not pw4_supervisor.processes()[1]:
                return True
        except Exception:
            return False
        await asyncio.sleep(0.05)
    return False


async def run_capture(keystore, address, send_silence=False):
    """Run one Audio Gateway session and return a CaptureReport."""
    deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS
    capture = Capture()
    hci_transport = None
    device = None
    acl = None
    rfcomm_client = None
    links = []
    silence_task = None
    phase = "port"
    try:
        hci_transport = await step(
            transport.open_transport(SERIAL_SPEC), deadline, PORT_TIMEOUT, phase)
        print("Bumble opened the Bluetooth UART.", flush=True)

        phase = "controller"
        config = DeviceConfiguration(
            name="Voice Bridge", classic_enabled=True, le_enabled=False,
            connectable=False, discoverable=False, classic_accept_any=False,
            classic_interlaced_scan_enabled=False, classic_smp_enabled=False,
            gap_service_enabled=False, gatt_service_enabled=False,
        )
        device = NoPairingDevice.from_config_with_hci(
            config, hci_transport.source, hci_transport.sink)
        device.keystore = keystore
        await step(device.power_on(), deadline, CONTROLLER_TIMEOUT, phase)
        print("The controller answered.", flush=True)

        phase = "connect"
        acl = await step(
            device.connect(address, transport=core.PhysicalTransport.BR_EDR),
            deadline, CONNECT_TIMEOUT, phase)
        acl.on("disconnection", capture.on_disconnect)
        print("The headset connected.", flush=True)

        phase = "authenticate"
        await step(acl.authenticate(), deadline, AUTH_TIMEOUT, phase)
        phase = "encrypt"
        await step(acl.encrypt(), deadline, ENCRYPT_TIMEOUT, phase)
        print("The link is authenticated and encrypted.", flush=True)

        phase = "sdp"
        record = await step(hfp.find_hf_sdp_record(acl), deadline, SDP_TIMEOUT, phase)
        if record is None:
            raise CaptureFailure("The headset has no Hands-Free service record.")

        phase = "rfcomm"
        rfcomm_client = rfcomm.Client(acl)
        mux = await step(rfcomm_client.start(), deadline, RFCOMM_TIMEOUT, phase)
        dlc = await step(mux.open_dlc(record[0]), deadline, RFCOMM_TIMEOUT, phase)

        phase = "slc"
        ag = CaptureAg(dlc)
        await step(ag.slc_ready.wait(), deadline, SLC_TIMEOUT, phase)
        await step(ag.cmer_received.wait(), deadline, SLC_TIMEOUT, phase)
        if capture.disconnect:
            raise CaptureFailure("The headset disconnected.")
        print("The HFP service link is ready.", flush=True)

        phase = "call"
        await step(simulate_call(ag), deadline, CALL_TIMEOUT, phase)

        phase = "sco"
        link = await step(setup_sco(device, acl, capture, links), deadline,
                          SCO_TIMEOUT, phase)
        capture.sco_opened_at = time.monotonic()
        print("The audio link is open. Recording.", flush=True)
        if send_silence:
            capture.silence_used = True
            silence_task = asyncio.create_task(
                send_silence(device, link, capture))

        phase = "capture"
        try:
            await asyncio.wait_for(
                capture.stop.wait(),
                remaining(deadline, DURATION + MAX_SETTLE_SECONDS + 1.0))
        except TimeoutError:
            pass

        if capture.invalid_status:
            raise CaptureFailure("The controller reported an invalid audio packet.")
        if capture.disconnect:
            raise CaptureFailure("The headset disconnected during the capture.")
        if not capture.settled:
            raise CaptureFailure("The headset did not stream audio.")
        if not capture.data:
            raise CaptureFailure("The capture received no audio data.")
        return CaptureReport(
            pcm=bytes(capture.data),
            settle_seconds=capture.settle_seconds,
            settle_after_sco_seconds=capture.settle_after_sco_seconds,
            silence_used=capture.silence_used,
            silence_error=capture.silence_error,
        )
    except CaptureFailure:
        raise
    except Exception as error:
        # Bumble exceptions can carry addresses or packet data, so the log gets
        # the type name only and the result line gets the step name only.
        print(f"Capture error: {type(error).__name__} during {PHASE_NAMES[phase]}.",
              file=sys.stderr, flush=True)
        raise CaptureFailure(f"The {PHASE_NAMES[phase]} step failed.") from error
    finally:
        capture.stop.set()
        if silence_task is not None:
            silence_task.cancel()
            await asyncio.gather(silence_task, return_exceptions=True)
            silence_task = None
        for link in links:
            link.sink = None
            await quiet(link.disconnect)
        if rfcomm_client is not None:
            await quiet(rfcomm_client.shutdown)
        if acl is not None:
            await quiet(acl.disconnect)
        if device is not None:
            await quiet(device.power_off)
        if hci_transport is not None:
            await quiet(hci_transport.close)
            if not await wait_for_uart_release():
                print("A process still holds the UART after the capture.",
                      file=sys.stderr, flush=True)


# -----------------------------------------------------------------------------
# Output and upload
# -----------------------------------------------------------------------------
def write_wav(pcm):
    """Write the PCM to a new private file in /tmp and return its path."""
    descriptor, path = tempfile.mkstemp(suffix=".wav", dir="/tmp")
    os.close(descriptor)
    try:
        with wave.open(path, "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(pcm)
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def report(message):
    """Print the one line that the reader plugin looks for."""
    print(f"Result: {message}", flush=True)


def headset_address(value):
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value):
        raise argparse.ArgumentTypeError("Enter a Bluetooth address in hex format.")
    return value.upper()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Record one headset clip on the Kindle and send it to the receiver.",
        epilog=(
            "This script is manual-start only. "
            "It stops the stock Bluetooth daemon and restores it in a finally path. "
            "Restoration is observed, not guaranteed. "
            "The link key is read from the pairing store and stays in memory. "
            "The recording may contain private speech."
        ),
    )
    parser.add_argument("--config", required=True, metavar="FILE",
                        help="Upload configuration with server_url and token. "
                             "The file is kept so later runs can reuse it.")
    parser.add_argument("--headset", type=headset_address, metavar="ADDRESS",
                        help="Headset Bluetooth address. Omit when the pairing store "
                             "holds one device.")
    parser.add_argument("--authorize-bluetooth-handoff", required=True,
                        action="store_true",
                        help="Confirm that you approve the Bluetooth handoff.")
    parser.add_argument("--authorize-microphone-recording", required=True,
                        action="store_true",
                        help="Confirm that you approve microphone recording.")
    parser.add_argument("--confirm-bluetooth-enabled-in-ui", required=True,
                        action="store_true",
                        help="Confirm that the Kindle UI shows Bluetooth enabled.")
    parser.add_argument("--send-silence", action="store_true",
                        help="Send paced silent audio to the headset during the capture. "
                             "This may make the headset start its microphone sooner. "
                             "The default is off.")
    return parser.parse_args(argv)


def raise_interrupt(signum, frame):
    raise pw4_supervisor.Interrupted(f"Signal {signum} stopped the run.")


def main(argv=None):
    args = parse_args(argv)
    for line in WARNINGS:
        print(line, flush=True)
    print("", flush=True)

    try:
        config = read_config(args.config)
        keystore, address = load_keystore(PAIRING_STORE, args.headset)
    except SetupError as error:
        print(str(error), file=sys.stderr, flush=True)
        report(f"not sent. {error}")
        return 2

    print(f"Headset: {address}", flush=True)
    print("The link key was read from the pairing store. It stays in memory.", flush=True)
    print(f"Downlink silence: {'on' if args.send_silence else 'off'}.", flush=True)

    experiment = pw4_supervisor.Experiment()
    capture_report = None
    failure = None
    old_handlers = {}
    try:
        for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, raise_interrupt)
        signal.alarm(TOTAL_SECONDS)
        experiment.prepare()
        print("Handing the Bluetooth UART to Bumble.", flush=True)
        experiment.handoff(budget=TOTAL_SECONDS + CLEANUP_RESERVE_SECONDS)
        experiment.open_uart(defer=True)
        print("The UART is free. Bumble owns the port now.", flush=True)
        capture_report = asyncio.run(run_capture(
            keystore, address, send_silence=args.send_silence))
    except pw4_supervisor.Interrupted as error:
        failure = str(error)
        print(f"Stopped: {error}", file=sys.stderr, flush=True)
    except CaptureFailure as error:
        failure = str(error)
        print(f"Capture failed: {error}", file=sys.stderr, flush=True)
    except Exception as error:
        failure = "The capture stopped on an unexpected error."
        print(f"Capture failed: {type(error).__name__}: {error}",
              file=sys.stderr, flush=True)
    finally:
        signal.alarm(0)
        experiment.cleanup()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    if experiment.cleanup_failed:
        print("Cleanup failed. Check Bluetooth on the device.", file=sys.stderr, flush=True)

    if failure is not None or capture_report is None or not capture_report.pcm:
        report_line = failure or 'The capture produced no audio.'
        report(f"not sent. {report_line}")
        return 1

    pcm = capture_report.pcm
    seconds = len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)
    print(f"Recorded {seconds:.2f} seconds of audio.", flush=True)
    print("Downlink silence used: "
          f"{'yes' if capture_report.silence_used else 'no'}.", flush=True)
    if capture_report.settle_seconds is not None:
        print(f"The audio settled after {capture_report.settle_seconds:.2f} "
              "seconds.", flush=True)
    if capture_report.settle_after_sco_seconds is not None:
        print("The audio settled "
              f"{capture_report.settle_after_sco_seconds:.2f} seconds after "
              "the SCO link opened.", flush=True)
    if capture_report.silence_error:
        print(capture_report.silence_error, file=sys.stderr, flush=True)

    path = None
    try:
        try:
            path = write_wav(pcm)
        except Exception as error:
            print(f"Could not write the WAV file: {type(error).__name__}.",
                  file=sys.stderr, flush=True)
            report("not sent. The WAV file could not be written.")
            return 1
        print(f"Wrote {path}.", flush=True)
        try:
            uploader.upload_recording(
                path, config["server_url"], config["token"],
                timeout=UPLOAD_TIMEOUT, allow_insecure_lan=True)
        except uploader.UploadError as error:
            print(f"Upload failed: {error}", file=sys.stderr, flush=True)
            report(f"not sent. The upload failed: {error}.")
            return 1
        except pw4_supervisor.Interrupted as error:
            print(f"Stopped: {error}", file=sys.stderr, flush=True)
            report("not sent. The run was stopped during the upload.")
            return 1
        except Exception:
            print("Upload failed on an unexpected error.", file=sys.stderr, flush=True)
            report("not sent. The upload failed. The receiver may hold the recording.")
            return 1
        print("The receiver stored the recording.", flush=True)
    finally:
        if path is not None:
            try:
                os.remove(path)
            except OSError:
                print(f"Could not delete {path}.", file=sys.stderr, flush=True)

    if experiment.cleanup_failed:
        report("sent. The Bluetooth restore failed.")
        return 0
    report("sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
