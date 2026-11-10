#!/usr/bin/env python3
"""Capture five seconds of HFP data through an authorized raw H:4 TCP relay.

The laptop listens on one specific IPv4 address. The Kindle connects out to it
and relays one controller. The laptop is an HFP Audio Gateway. The headset must
offer an HFP Hands-Free SDP record and initiate the service-level connection.

The controller is asked for 8 kHz, 16-bit linear PCM on the HCI path. It may
return encoded data instead. The output is raw HCI payload bytes, not a WAV.
Packet offsets and HCI status values go to a JSON sidecar, which never records
keys, HCI payloads, or addresses. The status values do not measure packets that
the controller never reports.

Every capture starts with about 2.4 seconds of stale or unfed SCO buffer
content: a large DC offset, a frozen tonal envelope, and a constant 31,898
plateau. The program discards packets until a sliding window of audio looks
live, then records the requested duration of usable audio. If the stream never
looks live within MAX_SETTLE_SECONDS it fails and writes no audio. --no-settle
restores the old fixed-duration behaviour. The detector assumes 16-bit mono at
8 kHz; if the controller returns encoded bytes its verdict is meaningless.

The listener does not authenticate the Kindle client. Bumble drops outgoing
packets while no client is attached, so the program creates the Device only
after a client connects. The Kindle permits about 210 seconds of experiment
after it connects; this program bounds the whole experiment below that limit.

Pairing, key transfer, retries, and controller recovery are outside this
prototype. An optional Bumble JSON key store is read only. The program does not
restore Kindle Bluetooth. Protect both output files with local filesystem
permissions: the raw file may contain private microphone data.
"""

import argparse
import asyncio
import errno
import ipaddress
import json
import logging
import math
import os
import re
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path

from bumble import core, hci, hfp, rfcomm, transport
from bumble.device import Device, DeviceConfiguration
from bumble.keys import JsonKeyStore, KeyStore, PairingKeys


DURATION = 5.0
# The requested HCI format is 16-bit mono PCM at 8 kHz. The settle detector
# converts packet lengths to seconds with this rate; it is unverified.
SAMPLE_RATE = 8000.0

# Settle detector. Until the headset's audio path streams, the SCO receive path
# returns stale or unfed buffer content. The three real captures in .data/ show
# three signatures, measured per 0.40 s window of 60-byte packets:
#   DC artifact   max |packet mean| 14,959..21,346; ZCR 0..148/s; RMS 70..1,037
#   31,898 plateau max |packet mean| 31,898..32,721; ZCR 0/s; RMS 0..1,037
#   live speech   max |packet mean| 113..641;      ZCR 1,057..2,921/s; RMS 161..943
# capture1's low-level noise burst (never settles) peaks at max |packet mean| 63,
# ZCR 1,670/s, RMS 22; capture2 is DC only. The thresholds sit between the two
# groups: 3.1x above the worst speech DC and 7.5x below the artifact, 1.8x below
# the quietest speech ZCR and 4x above the artifact, 2.7x below the quietest
# speech RMS and 2.8x above capture1's noise.
SETTLE_WINDOW_SECONDS = 0.40
SETTLE_MEAN_LIMIT = 2000.0
SETTLE_ZCR_MIN = 600.0
SETTLE_RMS_MIN = 60.0
# A stream that never looks live is a failure, not a recording. Bound the wait
# so the whole session stays inside EXPERIMENT_BUDGET.
MAX_SETTLE_SECONDS = 10.0
MAX_CAPTURE_SECONDS = DURATION + MAX_SETTLE_SECONDS

TRANSPORT_TIMEOUT = 20.0
# The Kindle supervisor allows 180 seconds to connect, so the laptop is the
# patient side. This wait starts before any experiment time is used.
CLIENT_WAIT_TIMEOUT = 180.0
# Covers listener startup and the client wait. It must stay above
# CLIENT_WAIT_TIMEOUT so the wait is never cut short.
STARTUP_TIMEOUT = 200.0
CLIENT_POLL_INTERVAL = 0.05
# The Kindle permits about 210 seconds after its client connects. Keep the
# whole experiment, including the capture, inside that window.
EXPERIMENT_BUDGET = 200.0
CONTROLLER_TIMEOUT = 90.0
SLC_TIMEOUT = 60.0
SCO_TIMEOUT = 25.0
CALL_INDICATOR_PAUSE = 0.5
CLEANUP_TIMEOUT = 3.0
MAX_BYTES = 1024 * 1024
MAX_PACKETS = 10000
READY_LINE = (
    "The HCI listener is ready. Start the Kindle relay now. "
    f"Waiting up to {CLIENT_WAIT_TIMEOUT:.0f} seconds for the Kindle client."
)
NOTICE = (
    "This program resets the controller. The HCI relay uses plaintext TCP.\n"
    "The listener accepts one unauthenticated Kindle HCI client.\n"
    "This program does not restore Kindle Bluetooth.\n"
    "The raw output may contain private microphone data. The format is unverified."
)


class ClientDisconnected(Exception):
    pass


class BudgetExpired(Exception):
    pass


class CaptureFailure(Exception):
    """A known failure with a stable, safe reason token."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Phase tokens recorded in the sidecar. The labels are for operator messages.
PHASE_LABELS = {
    "listener": "listener startup",
    "client_wait": "Kindle client wait",
    "controller_setup": "controller setup",
    "connect": "headset connection",
    "authenticate": "authentication",
    "encrypt": "encryption",
    "sdp": "SDP search",
    "rfcomm": "RFCOMM setup",
    "slc": "HFP SLC setup",
    "sco_setup": "SCO setup",
    "call_simulation": "call simulation",
    "capture": "capture",
}


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Argument values can contain addresses or private paths.
        print(NOTICE, file=sys.stderr)
        print("Invalid arguments. See --help.", file=sys.stderr)
        self.exit(2)


def ipv4_address(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise argparse.ArgumentTypeError("Enter a numeric IPv4 address.") from error
    if address.is_unspecified or address.is_multicast or int(address) == 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("Enter a specific unicast IPv4 address.")
    return str(address)


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Enter a port from 1 to 65535.") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Enter a port from 1 to 65535.")
    return port


def headset_address(value: str) -> hci.Address:
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value):
        raise argparse.ArgumentTypeError("Enter a Bluetooth address in hex format.")
    if value.upper() in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
        raise argparse.ArgumentTypeError("Enter a specific Bluetooth address.")
    return hci.Address(value, hci.Address.PUBLIC_DEVICE_ADDRESS)


def make_parser() -> argparse.ArgumentParser:
    parser = SafeParser(
        allow_abbrev=False,
        description="Listen for one Kindle HCI relay and record five seconds of raw HFP data.",
        epilog=(
            "This program does not restore Kindle Bluetooth. "
            "The listener does not authenticate the Kindle client. "
            "The controller may return encoded bytes despite the PCM request. "
            "The optional PCM silence stream is unverified. "
            "Pairing and key transfer are not supported. "
            "Some headsets may require an active call, which this program does not signal. "
            "Protect the output files with local filesystem permissions."
        ),
    )
    parser.add_argument("--host", required=True, type=ipv4_address, metavar="IP", help="Specific laptop Wi-Fi IPv4 address for the listener.")
    parser.add_argument("--port", required=True, type=port_number, metavar="PORT", help="Listener TCP port.")
    parser.add_argument("--headset", required=True, type=headset_address, metavar="ADDRESS", help="Headset Bluetooth address.")
    parser.add_argument("--output", required=True, type=Path, metavar="FILE", help="New raw output file. A .json sidecar is also created.")
    parser.add_argument("--keystore", type=Path, metavar="FILE", help="Existing local Bumble JSON key store. The program only reads it and searches all namespaces.")
    parser.add_argument("--send-pcm-silence", action="store_true", help="Send paced PCM silence. This option is unverified.")
    parser.add_argument("--no-settle", action="store_true", help="Disable the settle detector and record the leading artifact as before.")
    parser.add_argument("--simulate-call", action="store_true", help="Report an incoming call, then an active call, before SCO setup. Unverified.")
    parser.add_argument("--acknowledge-controller-reset", action="store_true", required=True, help="Accept the controller reset and configuration changes.")
    parser.add_argument("--acknowledge-unencrypted-hci", action="store_true", required=True, help="Accept the plaintext HCI relay risk.")
    parser.add_argument("--authorize-handoff", action="store_true", required=True, help="Confirm that you approve the device handoff.")
    parser.add_argument("--authorize-microphone-recording", action="store_true", required=True, help="Confirm that you approve microphone recording.")
    return parser


def connection_error_kind(error: Exception) -> str:
    if isinstance(error, socket.gaierror):
        return "address resolution failed"
    if isinstance(error, OSError):
        codes = {
            code for code in (error.errno, getattr(error, "winerror", None))
            if type(code) is int
        }
        if isinstance(error, ConnectionRefusedError) or codes & {errno.ECONNREFUSED, 10061}:
            return "connection refused"
        if codes & {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}:
            return "network or host unreachable"
        if codes & {errno.ENETDOWN, 10050}:
            return "network unavailable"
        if codes & {errno.EADDRINUSE, 10048}:
            return "listener address already in use"
        if codes & {errno.EADDRNOTAVAIL, 10049}:
            return "listener address not available"
        if isinstance(error, PermissionError) or codes & {errno.EACCES, errno.EPERM, 10013}:
            return "permission denied"
        if isinstance(error, ConnectionResetError) or codes & {errno.ECONNRESET, 10054}:
            return "connection reset"
        if isinstance(error, ConnectionAbortedError) or codes & {errno.ECONNABORTED, 10053}:
            return "connection aborted"
        if codes & {errno.ETIMEDOUT, 10060}:
            return "connection timed out"
        return "operating system error"
    return "unexpected error"


def classify_reason(error) -> str:
    """Return a safe reason token. Never returns keys, payloads, or addresses."""
    if isinstance(error, CaptureFailure):
        return error.reason
    if isinstance(error, core.BaseError):
        # HCI_Error and HCI_StatusError carry HCI_Constant.error_name() values,
        # for example PAGE_TIMEOUT_ERROR or COMMAND_DISALLOWED_ERROR.
        name = error.error_name
        if not name and error.error_code is not None:
            name = f"0x{error.error_code:02X}"
        if not name:
            return type(error).__name__
        if error.error_namespace in ("", "hci"):
            return name
        return f"{error.error_namespace}:{name}"
    if isinstance(error, (TimeoutError, core.TimeoutError)):
        return "timeout"
    if isinstance(error, core.CommandTimeoutError):
        return "command_timeout"
    if isinstance(error, OSError):
        return "os_error:" + connection_error_kind(error)
    if isinstance(error, core.BaseBumbleError):
        return type(error).__name__
    return "unclassified"


def describe_failure(error, phase, reason=None) -> dict:
    return {
        "phase": phase,
        "reason": reason or classify_reason(error),
        "exception": type(error).__name__ if error is not None else None,
    }


async def wait_for_client(sink):
    async with asyncio.timeout(CLIENT_WAIT_TIMEOUT):
        while True:
            client = sink.transport
            if client is not None:
                if client.is_closing():
                    raise ClientDisconnected()
                return client
            await asyncio.sleep(CLIENT_POLL_INTERVAL)


class ClientWatch:
    """Detect loss of the Kindle client and bound each experiment phase."""

    def __init__(self, sink, client):
        self.sink = sink
        self.client = client
        self.lost = asyncio.Event()
        self.task = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while not self.lost.is_set():
            current = self.sink.transport
            if current is not self.client or self.client.is_closing():
                self.lost.set()
                return
            await asyncio.sleep(CLIENT_POLL_INTERVAL)

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def guard(self, awaitable, timeout: float):
        if self.lost.is_set():
            raise ClientDisconnected()
        operation = asyncio.ensure_future(awaitable)
        lost = asyncio.ensure_future(self.lost.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, lost}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done:
                return operation.result()
            if lost in done and self.lost.is_set():
                raise ClientDisconnected()
            raise TimeoutError()
        finally:
            lost.cancel()
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, lost, return_exceptions=True)


class ReadOnlyKeys(KeyStore):
    """Find a peer key in any namespace of a Bumble JSON key store.

    Bumble looks keys up under str(device.public_address), for example
    'AA:BB:CC:DD:EE:FF/P'. An exported store may use another namespace, or no
    namespace at all. Only get() is implemented, so this wrapper cannot write.
    """

    def __init__(self, store: JsonKeyStore):
        self.store = store

    async def get(self, name: str):
        # load() reads the whole file and never writes it.
        db, _ = await self.store.load()
        peers = [(peer, entry) for keys in db.values() for peer, entry in keys.items()]
        plain = name.split("/", 1)[0]
        for peer, entry in peers:
            if peer == name:
                return PairingKeys.from_dict(entry)
        for peer, entry in peers:
            if peer.split("/", 1)[0] == plain:
                return PairingKeys.from_dict(entry)
        return None


class NoPairingDevice(Device):
    """Reject new pairing requests before Bumble's default pairing handlers run."""

    def _reject(self, command) -> None:
        async def send() -> None:
            try:
                await asyncio.wait_for(self.send_sync_command(command), CLEANUP_TIMEOUT)
            except Exception:
                pass
        asyncio.create_task(send())

    def on_authentication_io_capability_request(self, address) -> None:
        self._reject(hci.HCI_IO_Capability_Request_Negative_Reply_Command(
            bd_addr=address, reason=hci.HCI_PAIRING_NOT_ALLOWED_ERROR
        ))

    def on_authentication_user_confirmation_request(self, address, _code) -> None:
        self._reject(hci.HCI_User_Confirmation_Request_Negative_Reply_Command(bd_addr=address))

    def on_authentication_user_passkey_request(self, address) -> None:
        self._reject(hci.HCI_User_Passkey_Request_Negative_Reply_Command(bd_addr=address))

    def on_pin_code_request(self, address) -> None:
        self._reject(hci.HCI_PIN_Code_Request_Negative_Reply_Command(bd_addr=address))


class CaptureAg(hfp.AgProtocol):
    def __init__(self, dlc):
        # The DLC can deliver queued AT commands during the parent constructor.
        self.slc_ready = asyncio.Event()
        self.cmer_received = asyncio.Event()
        states = hfp.AgIndicatorState
        super().__init__(dlc, hfp.AgConfiguration(
            supported_ag_features=[],
            supported_ag_indicators=[states.service(), states.call(), states.callsetup(),
                                     states.callheld(), states.signal(), states.roam(), states.battchg()],
            supported_hf_indicators=[],
            supported_ag_call_hold_operations=[],
            supported_audio_codecs=[hfp.AudioCodec.CVSD],
        ))

    def _read_at(self, data: bytes) -> None:
        if len(self.read_buffer) + len(data) > 8192:
            raise CaptureFailure("hfp_command_buffer_limit")
        super()._read_at(data)

    def _check_remained_slc_commands(self) -> None:
        super()._check_remained_slc_commands()
        if not self._remained_slc_setup_features:
            self.slc_ready.set()

    def _on_cmer(self, *args) -> None:
        super()._on_cmer(*args)
        self.cmer_received.set()


class SettleDetector:
    """Decide when the SCO receive path starts delivering live audio.

    A sliding window over the packet callback rejects the three signatures of
    the startup artifact and accepts the first window that looks like live
    audio. The window is trimmed to at least SETTLE_WINDOW_SECONDS of samples,
    so the decision lags the window start by one window. settle_seconds names
    the window start, which is the first sample worth keeping.
    """

    def __init__(self, window_seconds: float = SETTLE_WINDOW_SECONDS,
                 mean_limit: float = SETTLE_MEAN_LIMIT,
                 zcr_min: float = SETTLE_ZCR_MIN,
                 rms_min: float = SETTLE_RMS_MIN,
                 sample_rate: float = SAMPLE_RATE):
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

    def feed(self, payload: bytes) -> bool:
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
                "window_seconds": round(window / self.sample_rate, 3),
            }
            return True
        return False


class Capture:
    def __init__(self, settle: bool = True):
        self.data = bytearray()
        self.packets = []
        self.status_counts = [0, 0, 0, 0]
        self.dropped_bytes = 0
        self.limit_hit = False
        self.invalid_status = False
        self.stop = asyncio.Event()
        self.disconnect = False
        self.started = None
        self.elapsed = None
        self.packets_received = 0
        self.settle_enabled = settle
        self.detector = SettleDetector() if settle else None
        self.settled = not settle
        self.settle_seconds = None
        self.settle_criterion = None
        self.discarded_packets = 0
        self.usable_samples = 0
        # Hold only the tail that a settle decision can still reach back to.
        self.pending = deque()
        self.pending_samples = 0
        self.pending_start = 0
        self.pending_limit = int((SETTLE_WINDOW_SECONDS + 0.5) * SAMPLE_RATE)

    def on_packet(self, packet) -> None:
        if self.stop.is_set():
            return
        status = int(packet.packet_status)
        if not 0 <= status <= 3:
            self.invalid_status = True
            self.stop.set()
            return
        self.status_counts[status] += 1
        self.packets_received += 1
        payload = packet.data
        if not self.settled:
            self._hold(payload, status)
            if self.detector.feed(payload):
                self._release()
            elif self.detector.samples_seen >= MAX_SETTLE_SECONDS * SAMPLE_RATE:
                self.stop.set()
            return
        self._store(payload, status)
        if self.usable_samples >= DURATION * SAMPLE_RATE:
            self.stop.set()

    def _hold(self, payload: bytes, status: int) -> None:
        self.pending.append((self.pending_start, payload, status))
        self.pending_start += len(payload) // 2
        self.pending_samples += len(payload) // 2
        while self.pending and self.pending_samples - len(self.pending[0][1]) // 2 >= self.pending_limit:
            old = self.pending.popleft()
            self.pending_samples -= len(old[1]) // 2
            self.discarded_packets += 1

    def _release(self) -> None:
        self.settled = True
        # Keep the exact sample boundary; the sidecar value is rounded only
        # for display.
        keep_from = int(round(self.detector.settle_seconds * SAMPLE_RATE))
        self.settle_seconds = round(self.detector.settle_seconds, 3)
        self.settle_criterion = self.detector.criterion
        held = list(self.pending)
        self.pending.clear()
        self.pending_samples = 0
        for start, payload, status in held:
            if start >= keep_from:
                self._store(payload, status)
            else:
                self.discarded_packets += 1

    def _store(self, payload: bytes, status: int) -> None:
        if len(self.packets) >= MAX_PACKETS or len(self.data) + len(payload) > MAX_BYTES:
            self.dropped_bytes += len(payload)
            self.limit_hit = True
            self.stop.set()
            return
        self.packets.append({"offset": len(self.data), "length": len(payload), "status": status})
        self.data.extend(payload)
        self.usable_samples += len(payload) // 2

    def on_disconnect(self, *_args) -> None:
        self.disconnect = True
        self.stop.set()


def remaining(deadline: float, cap: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise BudgetExpired()
    return min(cap, left)


async def pause_briefly(watch: "ClientWatch", deadline: float, seconds: float) -> None:
    """Sleep for a bounded time, with a small scheduling margin on the guard."""
    delay = remaining(deadline, seconds)
    await watch.guard(asyncio.sleep(delay), remaining(deadline, delay + 0.25))


async def simulate_call(ag: hfp.AgProtocol, watch: "ClientWatch", deadline: float) -> None:
    """Report an incoming call, then an active call, so the HF may open its mic.

    Uses AgProtocol.update_ag_indicator(indicator: hfp.AgIndicator, value: int).
    The pauses are bounded by the experiment deadline.
    """
    ag.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP, int(hfp.CallSetupAgIndicator.INCOMING_CALL_PROCESS)
    )
    await pause_briefly(watch, deadline, CALL_INDICATOR_PAUSE)
    ag.update_ag_indicator(hfp.AgIndicator.CALL, 1)
    ag.update_ag_indicator(
        hfp.AgIndicator.CALL_SETUP, int(hfp.CallSetupAgIndicator.NOT_IN_CALL_SETUP)
    )
    await pause_briefly(watch, deadline, CALL_INDICATOR_PAUSE)


async def send_silence(device, link, capture: Capture) -> None:
    # HFP CVSD S1 requests 16-bit mono PCM at 8000 samples per second.
    size = min(link.tx_packet_length, 240) & ~1
    if size < 2:
        capture.stop.set()
        raise RuntimeError("Invalid SCO transmit packet length")
    while not capture.stop.is_set():
        device.host.send_sco_sdu(link.handle, bytes(size))
        await asyncio.sleep(size / 16000.0)


async def cleanup_step(operation) -> bool:
    try:
        await asyncio.wait_for(operation(), CLEANUP_TIMEOUT)
        return True
    except Exception:
        return False


async def run_capture(args, capture: Capture) -> tuple[str, bool]:
    device = None
    acl = None
    rfcomm_client = None
    connected_links = []
    silence = None
    capture_timer = None
    watch = None
    hci_transport = None
    hci_client = None
    phase = "listener"
    failure = None
    result = "Capture failed."
    cleanup_ok = True
    # The settle detector needs the settle bound on top of the usable window.
    session_cap = MAX_CAPTURE_SECONDS if capture.settle_enabled else DURATION
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            async with asyncio.timeout(TRANSPORT_TIMEOUT):
                hci_transport = await transport.open_transport(f"tcp-server:{args.host}:{args.port}")
            sink, server = hci_transport.sink, hci_transport.server
            phase = "client_wait"
            print(READY_LINE, flush=True)
            hci_client = await wait_for_client(sink)
            server.close()
            if sink.transport is not hci_client or hci_client.is_closing():
                raise ClientDisconnected()
        watch = ClientWatch(sink, hci_client)
        watch.start()
        deadline = time.monotonic() + EXPERIMENT_BUDGET

        phase = "controller_setup"
        config = DeviceConfiguration(
            name="Voice Bridge", classic_enabled=True, le_enabled=False,
            connectable=False, discoverable=False, classic_accept_any=False,
            classic_interlaced_scan_enabled=False, classic_smp_enabled=False,
            gap_service_enabled=False, gatt_service_enabled=False,
        )
        device = NoPairingDevice.from_config_with_hci(config, hci_transport.source, hci_transport.sink)
        device.keystore = KeyStore()
        await watch.guard(device.power_on(), remaining(deadline, CONTROLLER_TIMEOUT))
        if args.keystore is not None:
            device.keystore = ReadOnlyKeys(JsonKeyStore.from_device(device, str(args.keystore)))

        phase = "connect"
        acl = await watch.guard(
            device.connect(args.headset, transport=core.PhysicalTransport.BR_EDR),
            remaining(deadline, CONTROLLER_TIMEOUT),
        )
        acl.on("disconnection", capture.on_disconnect)
        phase = "authenticate"
        await watch.guard(acl.authenticate(), remaining(deadline, CONTROLLER_TIMEOUT))
        phase = "encrypt"
        await watch.guard(acl.encrypt(), remaining(deadline, CONTROLLER_TIMEOUT))
        phase = "sdp"
        record = await watch.guard(
            hfp.find_hf_sdp_record(acl), remaining(deadline, CONTROLLER_TIMEOUT)
        )
        if record is None:
            raise CaptureFailure("no_hf_sdp_record")

        phase = "rfcomm"
        rfcomm_client = rfcomm.Client(acl)
        mux = await watch.guard(rfcomm_client.start(), remaining(deadline, SLC_TIMEOUT))
        dlc = await watch.guard(mux.open_dlc(record[0]), remaining(deadline, SLC_TIMEOUT))
        phase = "slc"
        ag = CaptureAg(dlc)
        await watch.guard(ag.slc_ready.wait(), remaining(deadline, SLC_TIMEOUT))
        await watch.guard(ag.cmer_received.wait(), remaining(deadline, SLC_TIMEOUT))
        if capture.disconnect:
            raise CaptureFailure("acl_disconnected")

        if args.simulate_call:
            phase = "call_simulation"
            await simulate_call(ag, watch, deadline)

        phase = "sco_setup"
        # Reserve the whole recording window, including the settle bound.
        if remaining(deadline, session_cap) < session_cap:
            raise BudgetExpired()

        loop = asyncio.get_running_loop()
        sco_result = loop.create_future()

        def on_sco(new_link) -> None:
            nonlocal capture_timer
            if new_link.acl_connection is not acl:
                return
            connected_links.append(new_link)
            if capture.started is None and not capture.stop.is_set():
                capture.started = time.monotonic()
                # A backstop only. The capture normally stops on usable samples.
                capture_timer = loop.call_later(session_cap, capture.stop.set)
                new_link.sink = capture.on_packet
                new_link.on("disconnection", capture.on_disconnect)
            if not sco_result.done():
                sco_result.set_result(new_link)

        def on_sco_failure(status) -> None:
            if not sco_result.done():
                reason = "sco_connection_failure:" + hci.HCI_Constant.error_name(status)
                sco_result.set_exception(CaptureFailure(reason))

        # Register before sending the command. Keep the link for cleanup even
        # if the command result or setup wait fails.
        device.on("sco_connection", on_sco)
        acl.on("sco_connection_failure", on_sco_failure)

        async def setup_sco():
            params = hfp.ESCO_PARAMETERS[hfp.DefaultCodecParameters.ESCO_CVSD_S1]
            command = hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
                connection_handle=acl.handle, **params.asdict()
            )
            await device.send_async_command(command, check_status=True)
            return await sco_result

        link = await watch.guard(setup_sco(), remaining(deadline, SCO_TIMEOUT))

        phase = "capture"
        if args.send_pcm_silence:
            silence = asyncio.create_task(send_silence(device, link, capture))
        await watch.guard(capture.stop.wait(), session_cap + 1.0)
        if silence is not None:
            if not silence.done():
                silence.cancel()
            try:
                await silence
            except asyncio.CancelledError:
                pass
            silence = None
        if capture.limit_hit:
            result = "Capture stopped at the memory limit."
            failure = describe_failure(None, phase, "memory_limit")
        elif capture.disconnect:
            result = "Capture stopped after a Bluetooth disconnect."
            failure = describe_failure(None, phase, "bluetooth_disconnect")
        elif capture.invalid_status:
            result = "Capture stopped after an invalid SCO status."
            failure = describe_failure(None, phase, "invalid_sco_status")
        elif capture.settle_enabled and not capture.settled:
            result = (
                "Capture discarded: the SCO stream never settled within "
                f"{MAX_SETTLE_SECONDS:.0f} seconds. No audio was written."
            )
            failure = describe_failure(None, phase, "stream_never_settled")
        elif not capture.data:
            result = "Capture received no SCO payload bytes."
            failure = describe_failure(None, phase, "no_sco_payload")
        else:
            result = "Capture completed. The audio format is not verified."
    except ClientDisconnected as error:
        result = f"Kindle HCI client disconnected during {PHASE_LABELS[phase]}. Capture aborted."
        failure = describe_failure(error, phase, "client_disconnected")
    except BudgetExpired as error:
        result = f"Experiment budget expired during {PHASE_LABELS[phase]}. Capture aborted."
        failure = describe_failure(error, phase, "budget_expired")
    except TimeoutError as error:
        failure = describe_failure(error, phase)
        if phase == "client_wait":
            result = "Listener timed out waiting for the Kindle HCI client."
        elif phase == "listener":
            result = "Listener startup timed out."
        else:
            result = f"Capture {PHASE_LABELS[phase]} timed out."
    except Exception as error:
        # Bumble exceptions can include addresses, keys, or packet data, so only
        # the classified reason and the type name are kept.
        failure = describe_failure(error, phase)
        if phase == "listener":
            result = f"Listener startup failed: {connection_error_kind(error)}."
        elif isinstance(error, OSError):
            result = f"Capture {PHASE_LABELS[phase]} failed: {connection_error_kind(error)}."
        else:
            result = f"Capture {PHASE_LABELS[phase]} failed."
    finally:
        if capture.started is not None:
            capture.elapsed = round(min(time.monotonic() - capture.started, session_cap), 3)
        capture.stop.set()
        if capture_timer is not None:
            capture_timer.cancel()
        if silence is not None:
            silence.cancel()
            try:
                await silence
            except (asyncio.CancelledError, Exception):
                pass
        closed_links = set()
        for sco in list(connected_links):
            sco.sink = None
            closed_links.add(id(sco))
            cleanup_ok = await cleanup_step(sco.disconnect) and cleanup_ok
        if rfcomm_client is not None:
            cleanup_ok = await cleanup_step(rfcomm_client.shutdown) and cleanup_ok
        if acl is not None:
            cleanup_ok = await cleanup_step(acl.disconnect) and cleanup_ok
        # A late SCO completion can arrive during the first cleanup steps.
        for sco in list(connected_links):
            if id(sco) not in closed_links:
                sco.sink = None
                cleanup_ok = await cleanup_step(sco.disconnect) and cleanup_ok
        if device is not None:
            cleanup_ok = await cleanup_step(device.power_off) and cleanup_ok
        if watch is not None:
            await watch.stop()
        server = getattr(hci_transport, "server", None) if hci_transport is not None else None
        if server is not None:
            try:
                server.close()
            except Exception:
                cleanup_ok = False
        if hci_transport is not None:
            try:
                if hci_client is None:
                    hci_client = getattr(getattr(hci_transport, "sink", None), "transport", None)
                if hci_client is not None:
                    hci_client.close()
            except Exception:
                cleanup_ok = False
            cleanup_ok = await cleanup_step(hci_transport.close) and cleanup_ok
            if server is not None:
                try:
                    await asyncio.wait_for(server.wait_closed(), CLEANUP_TIMEOUT)
                except Exception:
                    cleanup_ok = False
    return result, cleanup_ok, failure


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    print(NOTICE, file=sys.stderr)
    if args.keystore is not None and not args.keystore.is_file():
        print("The key store must be an existing file.", file=sys.stderr)
        return 2
    args.settle = not args.no_settle
    sidecar = Path(str(args.output) + ".json")
    try:
        raw = args.output.open("xb")
    except OSError:
        print("Cannot create the raw output file.", file=sys.stderr)
        return 2
    try:
        try:
            metadata = sidecar.open("x", encoding="utf-8")
        except OSError:
            print("Cannot create the metadata file. The raw file is empty.", file=sys.stderr)
            return 2
        with metadata:
            snooper = os.environ.pop("BUMBLE_SNOOPER", None)
            previous_disable = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            capture = None
            failure = None
            result = "Capture interrupted or failed."
            cleanup_ok = False
            try:
                async def run():
                    nonlocal capture
                    # Suppress background exception details from Bumble.
                    asyncio.get_running_loop().set_exception_handler(lambda _loop, _context: None)
                    capture = Capture(settle=args.settle)
                    return await run_capture(args, capture)
                try:
                    result, cleanup_ok, failure = asyncio.run(run())
                except KeyboardInterrupt:
                    result = "Capture interrupted. Cleanup was attempted."
                    failure = describe_failure(None, None, "interrupted")
                except Exception as error:
                    result = "Capture failed."
                    failure = describe_failure(error, None)
            finally:
                logging.disable(previous_disable)
                if snooper is not None:
                    os.environ["BUMBLE_SNOOPER"] = snooper
                if capture is not None:
                    raw.write(capture.data)
                json.dump({
                    "result": result,
                    "cleanup_ok": cleanup_ok,
                    "failure_phase": failure["phase"] if failure else None,
                    "failure_reason": failure["reason"] if failure else None,
                    "failure_exception": failure["exception"] if failure else None,
                    "listener": "tcp-server on one specific IPv4 address",
                    "client_wait_timeout_seconds": CLIENT_WAIT_TIMEOUT,
                    "experiment_budget_seconds": EXPERIMENT_BUDGET,
                    "requested_duration_seconds": DURATION,
                    "requested_usable_audio_seconds": DURATION,
                    "capture_wait_seconds": getattr(capture, "elapsed", None),
                    "settle_detection": "enabled" if args.settle else "disabled",
                    "settle_window_seconds": SETTLE_WINDOW_SECONDS,
                    "settle_thresholds": {
                        "max_abs_packet_dc": SETTLE_MEAN_LIMIT,
                        "min_zero_crossing_rate_per_second": SETTLE_ZCR_MIN,
                        "min_rms": SETTLE_RMS_MIN,
                    },
                    "max_settle_seconds": MAX_SETTLE_SECONDS,
                    "max_capture_seconds": MAX_CAPTURE_SECONDS,
                    "settle_detected_seconds": getattr(capture, "settle_seconds", None),
                    "packets_discarded_before_settle": (
                        capture.discarded_packets if capture else 0
                    ),
                    "packets_received": capture.packets_received if capture else 0,
                    "settle_criterion": getattr(capture, "settle_criterion", None),
                    "usable_audio_seconds": (
                        round(capture.usable_samples / SAMPLE_RATE, 3) if capture else 0.0
                    ),
                    "requested_hci_format": "linear PCM, 8000 Hz, 16-bit, two's complement; unverified",
                    "air_codec_requested": "CVSD, eSCO S1",
                    "actual_format": "unknown",
                    "pcm_silence_requested": args.send_pcm_silence,
                    "call_simulation": (
                        "incoming_call_then_active" if args.simulate_call else "none"
                    ),
                    "packet_status_counts_scope": (
                        "all received SCO packets, including discarded ones"
                    ),
                    "packet_status_counts": dict(zip(
                        ("correct", "possibly_invalid", "no_data", "partially_lost"),
                        capture.status_counts if capture else [0, 0, 0, 0],
                    )),
                    "packets": capture.packets if capture else [],
                    "bytes_saved": len(capture.data) if capture else 0,
                    "bytes_dropped_at_limit": capture.dropped_bytes if capture else 0,
                    "memory_limit_hit": capture.limit_hit if capture else False,
                    "invalid_status_seen": capture.invalid_status if capture else False,
                    "note": "HCI status does not count packets the controller did not report.",
                }, metadata, indent=2)
                metadata.write("\n")
    except OSError:
        print("Cannot write the output files. The files may be incomplete.", file=sys.stderr)
        return 1
    finally:
        raw.close()
    print(result)
    if failure:
        print(
            f"Failure: phase={failure['phase']} "
            f"reason={failure['reason']} exception={failure['exception']}"
        )
    if not cleanup_ok:
        print("Cleanup failed or timed out. Kindle Bluetooth was not restored.", file=sys.stderr)
    return 0 if cleanup_ok and result.startswith("Capture completed.") else 1


if __name__ == "__main__":
    sys.exit(main())
