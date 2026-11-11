#!/usr/bin/env python3
"""Keep one Bluetooth session open for repeated headset recordings.

This daemon is manual-start only. It performs the PW4 Bluetooth handoff once,
keeps Bumble powered on, and owns the Bluetooth UART for the whole session.
Each recording then opens and closes only the SCO audio link. The daemon does
not run the handoff again, does not call power_on again, and does not restore
Bluetooth between recordings.

The daemon listens on a Unix domain socket under /tmp. The protocol is one
ASCII line in and one ASCII line out:

    start_recording -> ok recording (SCO is open and its audio callback is installed)
    stop_recording  -> ok sent ... | error not sent. ...
    record          -> legacy fixed-duration one-shot capture and upload
    status          -> ok idle|preparing|recording|sending|stopping <link-state>
    phase           -> ok idle|preparing|recording|sending|stopping
    state           -> ok <phase> <elapsed_ms> <remaining_ms> <link> <result>
    result          -> most recent manual/one-shot result, or ok pending/none
    stop            -> ok stopping

The state command is the machine-readable form. elapsed_ms and remaining_ms
are -1 when the capture length is not known in advance, which is the case for
the legacy one-shot record command. result is - when no recording has finished
yet.

While the session runs, the daemon also rewrites the state file every 100 ms:

    v1 <heartbeat> <phase> <elapsed_ms> <remaining_ms> <link> <result>

The file is /tmp/voicebridge-bt-session.state, mode 0600. The heartbeat counts
up on every write, so a reader can tell a live session from a stale file. The
plugin uses this file for its live recording page instead of spawning a client
process for every update. The daemon removes the file when it stops.

Manual recording stores each valid SCO packet immediately. It does not create
or call a SettleDetector. Stop & send closes SCO, writes one WAV, uploads once,
and returns the result. A manual capture is bounded to 15 seconds and
stop_recording runs automatically at the cap. A separate phase query lets the
plugin show preparation, recording, and sending progress. The legacy record
command remains available for the fixed five-second convenience action.

Manual audio starts at SCO readiness. Its beginning may contain startup
artifact or silence because there is no settle wait. This is an intentional
trade-off for capturing the earliest audio.

--send-silence sends paced silent PCM downlink packets during each capture.
Some handsets may start their microphone only after downlink audio arrives.
The option is off by default. Legacy recordings also log settle time and the
time from SCO open to settle.

The socket is mode 0600. On Linux the daemon also checks SO_PEERCRED and
accepts only the same user ID. The Kindle kernel 4.1 supports AF_UNIX. All
processes on the Kindle may run as root, so the owner check is weak on this
device. Do not expose the socket over the network.

The link key is read from the Kindle pairing store at startup and held in
memory only. The daemon never writes the key to a file and never prints it.

The daemon restores Bluetooth in a finally path on stop, idle timeout, SIGTERM,
SIGINT, and an unexpected Python error. It cannot restore Bluetooth after
SIGKILL, SIGSEGV, a kernel hang, or a power loss. A crash can leave Bluetooth
unusable.
"""

import argparse
import asyncio
import os
import re
import signal
import socket
import stat
import struct
import sys
import time

try:
    import fcntl
except ImportError:  # Windows test hosts have no fcntl.
    fcntl = None


SOCKET_PATH = "/tmp/voicebridge-bt-session.sock"
STATE_PATH = "/tmp/voicebridge-bt-session.state"
COMMAND_PATH = "/tmp/voicebridge-bt-session.cmd"
STATE_INTERVAL_SECONDS = 0.1
CONTROL_TIMEOUT = 5.0
CONTROL_LINE_LIMIT = 1024
RECORD_CLIENT_TIMEOUT = 200.0
MAX_MANUAL_RECORD_SECONDS = 15.0
DEFAULT_IDLE_TIMEOUT = 1800
STARTUP_SECONDS = 120
CLEANUP_RESERVE_SECONDS = 30.0
LINK_DEADLINE_SECONDS = 90.0
CAPTURE_DEADLINE_SECONDS = 60.0

CLIENT_COMMANDS = (
    "record", "start_recording", "stop_recording", "status", "phase", "state",
    "result", "stop"
)

WARNINGS = (
    "This session is manual-start only.",
    "The session stops the stock Bluetooth daemon once at startup.",
    "A crash can leave Bluetooth unusable. D: access may disappear.",
    "Reboot is not guaranteed to restore Bluetooth.",
    "Cleanup does not prove that stock Bluetooth works.",
    "The watchdog cannot protect against SIGKILL or a hung kernel.",
    "The PID check and signal are not atomic. PID reuse can signal another process.",
    "The link key stays in process memory. The session never writes it to a file.",
    "The upload can use plain HTTP. The token and the audio are then visible on the network.",
    "Manual audio may begin with startup artifact or silence.",
    "The session restores Bluetooth when you stop it, after the idle time limit, or on an error.",
)


def load_modules():
    """Import the device modules only when the daemon starts.

    The client mode must work when Bumble is missing or broken, so the client
    path never imports capture_local.
    """
    if __package__:
        from . import capture_local, pw4_supervisor, uploader
    else:
        import capture_local
        import pw4_supervisor
        import uploader
    return capture_local, pw4_supervisor, uploader


def _uid():
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else 0


def unpack_record(outcome):
    """Return (pcm, settle, settle_after_sco, silence_used, silence_error).

    The daemon controller returns a CaptureReport. A test fake can return the
    older two-item tuple, so both shapes are accepted.
    """
    if isinstance(outcome, tuple):
        pcm = outcome[0]
        settle = outcome[1] if len(outcome) > 1 else None
        return pcm, settle, None, False, None
    return (outcome.pcm, outcome.settle_seconds, outcome.settle_after_sco_seconds,
            outcome.silence_used, outcome.silence_error)


def safe_text(value, limit=200):
    """Return one printable ASCII line with no control characters."""
    text = "".join(char if 32 <= ord(char) < 127 else " " for char in str(value))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text or "unknown"


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("Enter a whole number of seconds.")
    if number <= 0:
        raise argparse.ArgumentTypeError("Enter a number greater than zero.")
    return number


def positive_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("Enter a number of seconds.")
    if not number > 0:
        raise argparse.ArgumentTypeError("Enter a number greater than zero.")
    return number


def headset_address(value):
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value):
        raise argparse.ArgumentTypeError("Enter a Bluetooth address in hex format.")
    return value.upper()


class SessionLock:
    """One advisory lock per session, taken before any Bluetooth change."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self):
        if self.path is None or fcntl is None or self.fd is not None:
            return
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != _uid():
                raise RuntimeError("The session lock file is not owned by this user.")
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another Bluetooth session is running.") from error
        except Exception:
            os.close(fd)
            raise
        self.fd = fd

    def release(self):
        if self.fd is None:
            return
        fd = self.fd
        self.fd = None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


class Session:
    """One warm Bluetooth session with a line-based control channel."""

    def __init__(self, experiment, controller_factory, config, socket_path,
                 lock_path, idle_timeout, handoff_budget, upload_timeout,
                 capture_local, uploader, pw4_supervisor,
                 keep_latency=False, control_factory=None,
                 manual_max_duration=MAX_MANUAL_RECORD_SECONDS,
                 state_path=STATE_PATH, command_path=COMMAND_PATH):
        self.experiment = experiment
        self.controller_factory = controller_factory
        self.config = config
        self.socket_path = socket_path
        self.lock_path = lock_path
        self.idle_timeout = idle_timeout
        self.handoff_budget = handoff_budget
        self.upload_timeout = upload_timeout
        self.capture_local = capture_local
        self.uploader = uploader
        self.pw4_supervisor = pw4_supervisor
        self.keep_latency = keep_latency
        self.control_factory = control_factory or UnixControl
        self.lock = SessionLock(lock_path)
        self.control = None
        self.controller = None
        self.record_task = None
        self.recording = False
        self.record_kind = None
        self.record_state_lock = asyncio.Lock()
        self.manual_stop_event = None
        self.manual_max_duration = float(manual_max_duration)
        if self.manual_max_duration <= 0:
            raise ValueError("manual_max_duration must be greater than zero")
        self.state_path = state_path
        self.command_path = command_path
        self.state_task = None
        self.heartbeat = 0
        self.recording_started_at = None
        self.capture_limit = None
        self.last_record_result = None
        self.phase = "idle"
        self.latency_released = False
        self.stop_event = asyncio.Event()
        self.last_activity = time.monotonic()
        self.watchdog = None

    def prepare_cleanup(self):
        """Give the restore path a fresh, bounded time budget.

        The handoff budget covers the handoff and the session. A session can
        outlive that budget, so the cleanup path gets its own reserve. The
        supervisor's cleanup checks are unchanged.
        """
        if self.experiment.deadline is not None:
            self.experiment.deadline = time.monotonic() + CLEANUP_RESERVE_SECONDS

    def status_line(self):
        if self.controller is None:
            return "ok starting"
        if self.stop_event.is_set():
            state = "stopping"
        elif not self.recording:
            state = "idle"
        elif self.phase in ("preparing", "recording", "sending", "stopping"):
            state = self.phase
        else:
            state = "recording"
        link = "connected" if self.controller.connected else "disconnected"
        return f"ok {state} {link}"

    def set_phase(self, phase):
        # A settle notification can arrive after the capture ended. Never move
        # the phase back from sending or idle to recording.
        if phase == "recording" and self.phase in ("sending", "idle"):
            return
        self.phase = phase

    def phase_line(self):
        """Return the capture phase, so the client can cue the user."""
        if self.stop_event.is_set():
            return "ok stopping"
        if not self.recording:
            return "ok idle"
        return f"ok {self.phase}"

    def current_phase(self):
        if self.stop_event.is_set():
            return "stopping"
        if not self.recording:
            return "idle"
        return self.phase

    def capture_clock(self):
        """Return (elapsed_ms, remaining_ms) for the active capture.

        A manual capture has a known limit, so both values are exact. The
        legacy one-shot capture waits for the settle detector, so its length is
        not known in advance and both values are reported as -1.
        """
        if self.recording_started_at is None or self.capture_limit is None:
            return -1, -1
        elapsed = max(0.0, time.monotonic() - self.recording_started_at)
        remaining = max(0.0, self.capture_limit - elapsed)
        return int(round(elapsed * 1000.0)), int(round(remaining * 1000.0))

    def state_body(self):
        """Return one machine-readable state line without the ok prefix.

        The plugin reads this from the state file every 250 ms, so the fields
        are positional and the result text is last. A result may contain
        spaces but never a newline.
        """
        elapsed_ms, remaining_ms = self.capture_clock()
        if self.controller is None:
            link = "starting"
        else:
            link = "connected" if self.controller.connected else "disconnected"
        result = "-"
        if self.last_record_result is not None:
            result = safe_text(self.last_record_result, 300).replace("\n", " ")
        return (f"{self.current_phase()} {elapsed_ms} {remaining_ms} "
                f"{link} {result}")

    def state_line(self):
        return "ok " + self.state_body()

    def write_state_file(self):
        """Publish the state for the plugin. Best effort, never raises."""
        if not self.state_path:
            return
        self.heartbeat = (self.heartbeat + 1) % 1000000
        line = f"v1 {self.heartbeat} {self.state_body()}\n"
        temporary = self.state_path + ".tmp"
        try:
            with open(temporary, "w", encoding="ascii", errors="replace") as target:
                target.write(line)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.state_path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def remove_state_file(self):
        if not self.state_path:
            return
        try:
            os.unlink(self.state_path)
        except OSError:
            pass

    def poll_command_file(self):
        """Apply one queued command from the plugin.

        The plugin writes this file instead of starting a client process. A
        client process takes several seconds to start on the device, which is
        longer than the capture can wait.
        """
        if not self.command_path:
            return
        try:
            with open(self.command_path, "r", encoding="ascii",
                      errors="replace") as source:
                command = source.read(64).strip()
        except OSError:
            return
        try:
            os.unlink(self.command_path)
        except OSError:
            pass
        if not command:
            return
        if command == "stop_recording":
            event = self.manual_stop_event
            if event is not None and self.recording:
                event.set()

    def remove_command_file(self):
        if not self.command_path:
            return
        try:
            os.unlink(self.command_path)
        except OSError:
            pass

    async def state_loop(self):
        while True:
            self.write_state_file()
            self.poll_command_file()
            await asyncio.sleep(STATE_INTERVAL_SECONDS)

    async def dispatch(self, line):
        """Return (reply, stop_after) for one control line."""
        command = line.strip().lower()
        if command == "status":
            return self.status_line(), False
        if command == "phase":
            return self.phase_line(), False
        if command == "state":
            return self.state_line(), False
        if command == "result":
            return self.result_line(), False
        if command == "stop":
            return "ok stopping", True
        if command == "record":
            return await self.record_line(), False
        if command == "start_recording":
            return await self.start_recording_line(), False
        if command == "stop_recording":
            return await self.stop_recording_line(), False
        if not command:
            return "error empty command", False
        return "error unknown command", False

    def result_line(self):
        if self.recording:
            return "ok pending"
        if self.last_record_result is None:
            return "ok none"
        return "ok result " + safe_text(self.last_record_result, 300)

    async def record_line(self):
        async with self.record_state_lock:
            if self.stop_event.is_set():
                return "error stopping. The session is stopping."
            if self.recording:
                return "error busy. A recording is already running."
            self.recording = True
            self.record_kind = "one-shot"
            self.manual_stop_event = None
            self.last_record_result = None
            self.set_phase("preparing")
            task = asyncio.create_task(self._record())
            self.record_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            return "error stopped. The recording was stopped."

    async def start_recording_line(self):
        """Prepare SCO and reply as soon as its audio callback is installed."""
        loop = asyncio.get_running_loop()
        async with self.record_state_lock:
            if self.stop_event.is_set():
                return "error stopping. The session is stopping."
            if self.recording:
                return "error busy. A recording is already running."
            ready = loop.create_future()
            stop_requested = asyncio.Event()
            self.recording = True
            self.record_kind = "manual"
            self.manual_stop_event = stop_requested
            self.last_record_result = None
            self.set_phase("preparing")
            task = asyncio.create_task(
                self._manual_recording(ready, stop_requested))
            self.record_task = task
        return await asyncio.shield(ready)

    async def stop_recording_line(self):
        """Stop the active manual capture and await its one upload result."""
        async with self.record_state_lock:
            if self.stop_event.is_set():
                return "error stopping. The Bluetooth session is stopping."
            if not self.recording or self.record_task is None:
                return "error not recording. There is no manual recording to stop."
            if self.record_kind != "manual":
                return "error busy. The active recording is not a manual recording."
            task = self.record_task
            if self.manual_stop_event is not None:
                self.manual_stop_event.set()
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            return "error stopped. The Bluetooth session stopped the recording."

    async def _finish_recording(self, task, reply):
        async with self.record_state_lock:
            if self.record_task is task:
                self.last_record_result = reply
                self.record_task = None
                self.recording = False
                self.record_kind = None
                self.manual_stop_event = None
                self.recording_started_at = None
                self.capture_limit = None
                self.set_phase("idle")
                self.last_activity = time.monotonic()
            self.write_state_file()

    async def _record(self):
        task = asyncio.current_task()
        reply = "error not sent. The recording failed."
        latency_held = False
        try:
            if not self.keep_latency:
                self.experiment.acquire_latency()
                self.latency_released = False
                latency_held = True
            try:
                outcome = await self.controller.record()
            finally:
                if latency_held:
                    self.experiment.release_latency()
                    self.latency_released = True
                    latency_held = False
            pcm, settle_seconds, settle_after_sco, silence_used, silence_error = (
                unpack_record(outcome))
            self.set_phase("sending")
            await asyncio.sleep(0)
            reply = await self._upload_pcm(
                pcm, settle_seconds, settle_after_sco, silence_used, silence_error)
        except self.capture_local.CaptureFailure as error:
            message = safe_text(str(error))
            print(f"Record failed: {message}", file=sys.stderr, flush=True)
            reply = "error not sent. " + message
        except self.pw4_supervisor.Interrupted as error:
            message = safe_text(str(error))
            print(f"Record stopped: {message}", file=sys.stderr, flush=True)
            reply = "error not sent. " + message
        except self.uploader.UploadError as error:
            message = safe_text(str(error))
            print(f"Upload failed: {message}", file=sys.stderr, flush=True)
            reply = "error not sent. The upload failed: " + message
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Bumble exceptions can carry addresses or packet data. Log only
            # the type name.
            print(f"Record failed: {type(error).__name__}.", file=sys.stderr, flush=True)
            reply = "error not sent. The recording failed."
        finally:
            if latency_held:
                try:
                    self.experiment.release_latency()
                    self.latency_released = True
                except Exception:
                    pass
            await self._finish_recording(task, reply)
        return reply

    async def _manual_recording(self, ready, stop_requested):
        """Own one manual capture from SCO preparation through upload."""
        task = asyncio.current_task()
        reply = "error not sent. The recording failed."
        capture = None
        capture_started = False
        latency_held = False
        max_duration_reached = False
        cancelled = False
        try:
            if not self.keep_latency:
                self.experiment.acquire_latency()
                self.latency_released = False
                latency_held = True
            if self.controller is None:
                raise self.capture_local.CaptureFailure(
                    "The Bluetooth session is not ready.")
            capture = await self.controller.start_recording(
                max_duration=self.manual_max_duration)
            capture_started = True
            # SCO is open and its callback is installed. This is the moment the
            # microphone is live, so it is the moment the clock starts.
            self.recording_started_at = time.monotonic()
            self.capture_limit = self.manual_max_duration
            self.set_phase("recording")
            if not ready.done():
                ready.set_result("ok recording")

            capture_wait = asyncio.create_task(capture.stop.wait())
            stop_wait = asyncio.create_task(stop_requested.wait())
            done = set()
            try:
                done, _pending = await asyncio.wait(
                    (capture_wait, stop_wait),
                    timeout=self.manual_max_duration,
                    return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (capture_wait, stop_wait):
                    if not waiter.done():
                        waiter.cancel()
                await asyncio.gather(capture_wait, stop_wait,
                                     return_exceptions=True)
            max_duration_reached = not done or bool(
                getattr(capture, "max_duration_reached", False))
            if max_duration_reached:
                try:
                    capture.max_duration_reached = True
                except AttributeError:
                    pass

            self.set_phase("sending")
            await asyncio.sleep(0)
            outcome = await self.controller.stop_recording()
            capture_started = False
            pcm, settle_seconds, settle_after_sco, silence_used, silence_error = (
                unpack_record(outcome))
            if latency_held:
                self.experiment.release_latency()
                self.latency_released = True
                latency_held = False
            reply = await self._upload_pcm(
                pcm, settle_seconds, settle_after_sco, silence_used, silence_error,
                max_duration_reached=max_duration_reached)
        except self.capture_local.CaptureFailure as error:
            message = safe_text(str(error))
            if max_duration_reached:
                message += f" The {self.manual_max_duration:g}-second maximum was reached."
            print(f"Record failed: {message}", file=sys.stderr, flush=True)
            reply = "error not sent. " + message
        except self.pw4_supervisor.Interrupted as error:
            message = safe_text(str(error))
            print(f"Record stopped: {message}", file=sys.stderr, flush=True)
            reply = "error not sent. " + message
        except self.uploader.UploadError as error:
            message = safe_text(str(error))
            print(f"Upload failed: {message}", file=sys.stderr, flush=True)
            reply = "error not sent. The upload failed: " + message
        except asyncio.CancelledError:
            cancelled = True
            reply = "error stopped. The Bluetooth session stopped the recording."
            if capture_started and self.controller is not None:
                try:
                    await self.controller.stop_recording()
                except Exception:
                    pass
                capture_started = False
        except Exception as error:
            print(f"Record failed: {type(error).__name__}.", file=sys.stderr, flush=True)
            reply = "error not sent. The recording failed."
        finally:
            if capture_started and self.controller is not None:
                try:
                    await self.controller.stop_recording()
                except Exception:
                    pass
            if latency_held:
                try:
                    self.experiment.release_latency()
                    self.latency_released = True
                except Exception:
                    pass
            if not ready.done():
                ready.set_result(reply)
            await self._finish_recording(task, reply)
        if cancelled:
            raise asyncio.CancelledError
        return reply

    async def _upload_pcm(self, pcm, settle_seconds, settle_after_sco,
                          silence_used, silence_error,
                          max_duration_reached=False):
        if not pcm:
            raise self.capture_local.CaptureFailure(
                "The capture produced no audio data.")
        self.log_record(pcm, settle_seconds, settle_after_sco,
                        silence_used, silence_error)
        path = self.capture_local.write_wav(pcm)
        try:
            upload_task = asyncio.create_task(asyncio.to_thread(
                self.uploader.upload_recording,
                path, self.config["server_url"], self.config["token"],
                timeout=self.upload_timeout, allow_insecure_lan=True))
            try:
                result = await asyncio.shield(upload_task)
            except asyncio.CancelledError:
                # Keep the temporary WAV until the worker has stopped using it.
                try:
                    await asyncio.shield(upload_task)
                except Exception:
                    pass
                raise
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return self.format_result(
            result, len(pcm), settle_seconds,
            max_duration_reached=max_duration_reached)

    def log_record(self, pcm, settle_seconds, settle_after_sco,
                   silence_used, silence_error):
        """Write one measurement line for the capture latency experiment."""
        seconds = len(pcm) / (self.capture_local.SAMPLE_RATE *
                              self.capture_local.SAMPLE_WIDTH)
        parts = [f"Record: silence {'on' if silence_used else 'off'}",
                 f"kept {seconds:.2f} s"]
        if settle_seconds is not None:
            parts.append(f"settle {settle_seconds:.2f} s")
        if settle_after_sco is not None:
            parts.append(f"{settle_after_sco:.2f} s after the SCO link opened")
        print(", ".join(parts) + ".", flush=True)
        if silence_error:
            print(f"Downlink silence: {safe_text(silence_error)}",
                  file=sys.stderr, flush=True)

    def format_result(self, result, samples, settle_seconds,
                      max_duration_reached=False):
        seconds = samples / (self.capture_local.SAMPLE_RATE * self.capture_local.SAMPLE_WIDTH)
        parts = ["ok", "sent"]
        recording_id = result.get("id") if isinstance(result, dict) else None
        if (isinstance(recording_id, str) and recording_id and
                self.config["token"] not in recording_id):
            parts.append(safe_text(recording_id, 80))
        parts.append(f"{seconds:.2f}s")
        if settle_seconds is not None:
            parts.append(f"settle {settle_seconds:.2f}s")
        if max_duration_reached:
            parts.extend(("max", "duration", "reached"))
        return " ".join(parts)

    async def idle_watchdog(self):
        interval = min(1.0, max(0.05, self.idle_timeout / 4.0))
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(self.stop_event.wait(), interval)
                return
            except TimeoutError:
                pass
            if self.recording:
                continue
            if time.monotonic() - self.last_activity >= self.idle_timeout:
                print("The session idle time limit expired.", flush=True)
                self.stop_event.set()
                return

    async def _cancel_record(self):
        task = self.record_task
        if task is None or task.done():
            return
        if self.phase == "sending":
            # An in-flight HTTP request may already have reached the receiver.
            # Let it finish once before restoring Bluetooth.
            await asyncio.gather(task, return_exceptions=True)
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _cancel_startup_alarm(self):
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

    async def run(self):
        try:
            self.lock.acquire()
        except RuntimeError as error:
            print(f"The session did not start: {error}", file=sys.stderr, flush=True)
            raise
        self.experiment.prepare()
        self.experiment.handoff(budget=self.handoff_budget)
        self.experiment.open_uart(defer=True)
        self.controller = self.controller_factory()
        try:
            self.controller.on_phase = self.set_phase
        except AttributeError:
            pass
        try:
            await self.controller.start()
            if not self.keep_latency:
                self.experiment.release_latency()
                self.latency_released = True
            self.control = self.control_factory(self)
            try:
                await self.control.start()
                self._cancel_startup_alarm()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    try:
                        loop.add_signal_handler(sig, self.stop_event.set)
                    except (NotImplementedError, RuntimeError):
                        pass
                self.last_activity = time.monotonic()
                self.watchdog = asyncio.create_task(self.idle_watchdog())
                self.state_task = asyncio.create_task(self.state_loop())
                try:
                    await self.stop_event.wait()
                finally:
                    self.watchdog.cancel()
                    await asyncio.gather(self.watchdog, return_exceptions=True)
                    await self._cancel_record()
                    if self.state_task is not None:
                        self.state_task.cancel()
                        await asyncio.gather(self.state_task, return_exceptions=True)
                        self.state_task = None
                    self.remove_state_file()
                    self.remove_command_file()
            finally:
                if self.control is not None:
                    await self.control.close()
        finally:
            if not self.keep_latency and self.latency_released:
                try:
                    self.experiment.acquire_latency()
                    self.latency_released = False
                except Exception:
                    pass
            await self.controller.close()


class UnixControl:
    """Owner-only Unix domain socket with one line per request."""

    def __init__(self, session):
        self.session = session
        self.server = None

    async def start(self):
        try:
            self._remove_stale_socket()
            self.server = await asyncio.start_unix_server(
                self.handle_client, path=self.session.socket_path,
                limit=CONTROL_LINE_LIMIT)
            try:
                os.chmod(self.session.socket_path, 0o600)
            except OSError:
                pass
        except Exception:
            await self.close()
            raise

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self._remove_socket()

    def _remove_stale_socket(self):
        try:
            info = os.lstat(self.session.socket_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("The control path exists and is not a socket.")
        if info.st_uid != _uid():
            raise RuntimeError("The control socket belongs to another user.")
        os.unlink(self.session.socket_path)

    def _remove_socket(self):
        try:
            info = os.lstat(self.session.socket_path)
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == _uid():
            try:
                os.unlink(self.session.socket_path)
            except FileNotFoundError:
                pass

    def _peer_allowed(self, writer):
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            return True
        try:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                  struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
        except OSError:
            return False
        return uid == _uid()

    async def _write(self, writer, reply):
        try:
            writer.write((reply + "\n").encode("ascii", "replace"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def handle_client(self, reader, writer):
        try:
            if not self._peer_allowed(writer):
                await self._write(writer, "error access denied")
                return
            try:
                line = await asyncio.wait_for(reader.readline(), CONTROL_TIMEOUT)
            except TimeoutError:
                await self._write(writer, "error the command timed out")
                return
            except ValueError:
                await self._write(writer, "error the command is too long")
                return
            except (ConnectionError, OSError):
                return
            if len(line) > CONTROL_LINE_LIMIT:
                await self._write(writer, "error the command is too long")
                return
            try:
                reply, stop_after = await self.session.dispatch(
                    line.decode("ascii", "replace"))
            except Exception as error:
                print(f"Control error: {type(error).__name__}.", file=sys.stderr, flush=True)
                await self._write(writer, "error the session failed")
                return
            await self._write(writer, reply)
            if stop_after:
                self.session.stop_event.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class BumbleController:
    """One powered Bumble controller that survives across recordings."""

    def __init__(self, capture_local, keystore, address, send_silence=False):
        self.capture_local = capture_local
        self.keystore = keystore
        self.address = address
        self.send_silence = send_silence
        self.on_phase = None
        self.transport = None
        self.device = None
        self.acl = None
        self.acl_alive = False
        self.rfcomm_client = None
        self.ag = None
        self._capture = None
        self._capture_links = []
        self._silence_task = None

    @property
    def connected(self):
        return self.acl is not None and self.acl_alive

    def _notify_phase(self, phase):
        callback = self.on_phase
        if callback is not None:
            callback(phase)

    async def _announce_settle(self, capture):
        await capture.settled_event.wait()
        self._notify_phase("recording")

    def _on_acl_disconnect(self, *_args):
        self.acl_alive = False
        self.ag = None
        if self._capture is not None:
            self._capture.on_disconnect()

    def _on_dlc_close(self, *_args):
        # The ACL can stay alive after the HFP service link closes. Drop the
        # stale AG so the next record rebuilds the RFCOMM and SLC layers.
        self.ag = None
        if self._capture is not None:
            self._capture.on_disconnect()

    async def start(self):
        try:
            self.transport = await asyncio.wait_for(
                self.capture_local.transport.open_transport(self.capture_local.SERIAL_SPEC),
                self.capture_local.PORT_TIMEOUT)
            config = self.capture_local.DeviceConfiguration(
                name="Voice Bridge", classic_enabled=True, le_enabled=False,
                connectable=False, discoverable=False, classic_accept_any=False,
                classic_interlaced_scan_enabled=False, classic_smp_enabled=False,
                gap_service_enabled=False, gatt_service_enabled=False,
            )
            self.device = self.capture_local.NoPairingDevice.from_config_with_hci(
                config, self.transport.source, self.transport.sink)
            self.device.keystore = self.keystore
            await asyncio.wait_for(self.device.power_on(),
                                   self.capture_local.CONTROLLER_TIMEOUT)
        except Exception:
            await self.close()
            raise

    async def ensure_link(self, deadline):
        if self.acl is not None and self.acl_alive and self.ag is not None:
            return
        await self._drop_link()
        phase = "connect"
        try:
            self.acl = await self.capture_local.step(
                self.device.connect(
                    self.address,
                    transport=self.capture_local.core.PhysicalTransport.BR_EDR),
                deadline, self.capture_local.CONNECT_TIMEOUT, phase)
            self.acl_alive = True
            self.acl.on("disconnection", self._on_acl_disconnect)
            phase = "authenticate"
            await self.capture_local.step(
                self.acl.authenticate(), deadline,
                self.capture_local.AUTH_TIMEOUT, phase)
            phase = "encrypt"
            await self.capture_local.step(
                self.acl.encrypt(), deadline,
                self.capture_local.ENCRYPT_TIMEOUT, phase)
            phase = "sdp"
            record = await self.capture_local.step(
                self.capture_local.hfp.find_hf_sdp_record(self.acl), deadline,
                self.capture_local.SDP_TIMEOUT, phase)
            if record is None:
                raise self.capture_local.CaptureFailure(
                    "The headset has no Hands-Free service record.")
            phase = "rfcomm"
            self.rfcomm_client = self.capture_local.rfcomm.Client(self.acl)
            mux = await self.capture_local.step(
                self.rfcomm_client.start(), deadline,
                self.capture_local.RFCOMM_TIMEOUT, phase)
            dlc = await self.capture_local.step(
                mux.open_dlc(record[0]), deadline,
                self.capture_local.RFCOMM_TIMEOUT, phase)
            phase = "slc"
            self.ag = self.capture_local.CaptureAg(dlc)
            dlc.on("close", self._on_dlc_close)
            await self.capture_local.step(
                self.ag.slc_ready.wait(), deadline,
                self.capture_local.SLC_TIMEOUT, phase)
            await self.capture_local.step(
                self.ag.cmer_received.wait(), deadline,
                self.capture_local.SLC_TIMEOUT, phase)
        except self.capture_local.CaptureFailure:
            await self._drop_link()
            raise
        except Exception as error:
            print(f"Link error: {type(error).__name__} during "
                  f"{self.capture_local.PHASE_NAMES[phase]}.", file=sys.stderr, flush=True)
            await self._drop_link()
            raise self.capture_local.CaptureFailure(
                f"The {self.capture_local.PHASE_NAMES[phase]} step failed.") from error

    async def _open_sco(self, capture, links):
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def on_sco(new_link):
            if new_link.acl_connection is not self.acl:
                return
            links.append(new_link)
            if not capture.stop.is_set():
                new_link.sink = capture.on_packet
                new_link.on("disconnection", capture.on_disconnect)
            if not result.done():
                result.set_result(new_link)

        def on_failure(_status):
            if not result.done():
                result.set_exception(self.capture_local.CaptureFailure(
                    "The audio link failed to open."))

        self.device.on("sco_connection", on_sco)
        self.acl.on("sco_connection_failure", on_failure)
        try:
            params = self.capture_local.hfp.ESCO_PARAMETERS[
                self.capture_local.hfp.DefaultCodecParameters.ESCO_CVSD_S1]
            command = self.capture_local.hci.HCI_Enhanced_Setup_Synchronous_Connection_Command(
                connection_handle=self.acl.handle, **params.asdict())
            await self.device.send_async_command(command, check_status=True)
            return await result
        finally:
            self.device.remove_listener("sco_connection", on_sco)
            if self.acl is not None:
                self.acl.remove_listener("sco_connection_failure", on_failure)
            if not result.done():
                result.cancel()

    async def _end_call(self):
        if self.ag is None:
            return
        try:
            self.ag.update_ag_indicator(self.capture_local.hfp.AgIndicator.CALL, 0)
            self.ag.update_ag_indicator(
                self.capture_local.hfp.AgIndicator.CALL_SETUP,
                int(self.capture_local.hfp.CallSetupAgIndicator.NOT_IN_CALL_SETUP))
            await asyncio.sleep(self.capture_local.CALL_INDICATOR_PAUSE)
        except Exception:
            pass

    async def start_recording(self, max_duration=MAX_MANUAL_RECORD_SECONDS):
        """Open SCO and return after its first-packet callback is installed."""
        if self._capture is not None:
            raise self.capture_local.CaptureFailure(
                "A recording is already running.")
        link_deadline = time.monotonic() + LINK_DEADLINE_SECONDS
        await self.ensure_link(link_deadline)
        if self.ag is None:
            raise self.capture_local.CaptureFailure(
                "The HFP service link is not ready.")
        deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS
        capture = self.capture_local.Capture(
            settle=False, max_duration=max_duration)
        self._capture = capture
        self._capture_links = []
        phase = "call"
        try:
            await self.capture_local.step(
                self.capture_local.simulate_call(self.ag), deadline,
                self.capture_local.CALL_TIMEOUT, phase)
            phase = "sco"
            link = await self.capture_local.step(
                self._open_sco(capture, self._capture_links), deadline,
                self.capture_local.SCO_TIMEOUT, phase)
            capture.sco_opened_at = time.monotonic()
            if self.send_silence:
                capture.silence_used = True
                self._silence_task = asyncio.create_task(
                    self.capture_local.send_silence(self.device, link, capture))
            # SCO open means the local link is ready, not that the headset is
            # sending audio. Cue only once the microphone is converting, so the
            # first thing the user says is not lost.
            try:
                await asyncio.wait_for(
                    capture.audio_event.wait(),
                    self.capture_local.AUDIO_READY_TIMEOUT)
            except TimeoutError:
                capture.force_cue()
                print("Audio: no signal before the deadline; cueing anyway.",
                      file=sys.stderr, flush=True)
            self._notify_phase("recording")
            return capture
        except asyncio.CancelledError:
            await self._cleanup_capture(capture)
            raise
        except self.capture_local.CaptureFailure:
            await self._cleanup_capture(capture)
            raise
        except Exception as error:
            await self._cleanup_capture(capture)
            print(f"Link error: {type(error).__name__} during "
                  f"{self.capture_local.PHASE_NAMES[phase]}.",
                  file=sys.stderr, flush=True)
            raise self.capture_local.CaptureFailure(
                f"The {self.capture_local.PHASE_NAMES[phase]} step failed.") from error

    async def stop_recording(self):
        """Close the current SCO audio capture and return its PCM report."""
        capture = self._capture
        if capture is None:
            raise self.capture_local.CaptureFailure(
                "There is no active manual recording.")
        failure = None
        if capture.invalid_status:
            failure = "The controller reported an invalid audio packet."
        elif capture.disconnect:
            failure = "The headset disconnected during the recording."
        elif not capture.data:
            failure = "The headset did not stream audio."
        line = capture.arrival_line()
        if line:
            print(line, flush=True)
        report = self.capture_local.CaptureReport(
            pcm=bytes(capture.data[capture.cue_offset or 0:]),
            settle_seconds=None,
            settle_after_sco_seconds=None,
            silence_used=capture.silence_used,
            silence_error=capture.silence_error,
        )
        await self._cleanup_capture(capture)
        if failure is not None:
            raise self.capture_local.CaptureFailure(failure)
        return report

    async def _cleanup_capture(self, capture):
        """Close SCO and end the simulated HFP call, keeping the warm link."""
        capture.stop.set()
        if self._capture is capture:
            self._capture = None
        silence_task = self._silence_task
        self._silence_task = None
        if silence_task is not None:
            silence_task.cancel()
            await asyncio.gather(silence_task, return_exceptions=True)
        links = self._capture_links
        self._capture_links = []
        for link in links:
            link.sink = None
            await self.capture_local.quiet(link.disconnect)
        await self._end_call()

    async def record(self):
        link_deadline = time.monotonic() + LINK_DEADLINE_SECONDS
        await self.ensure_link(link_deadline)
        if self.ag is None:
            raise self.capture_local.CaptureFailure(
                "The HFP service link is not ready.")
        deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS
        capture = self.capture_local.Capture()
        self._capture = capture
        links = []
        settle_task = None
        silence_task = None
        try:
            await self.capture_local.step(
                self.capture_local.simulate_call(self.ag), deadline,
                self.capture_local.CALL_TIMEOUT, "call")
            link = await self.capture_local.step(
                self._open_sco(capture, links), deadline,
                self.capture_local.SCO_TIMEOUT, "sco")
            capture.sco_opened_at = time.monotonic()
            self._notify_phase("preparing")
            settle_task = asyncio.create_task(self._announce_settle(capture))
            if self.send_silence:
                capture.silence_used = True
                silence_task = asyncio.create_task(
                    self.capture_local.send_silence(self.device, link, capture))
            try:
                await asyncio.wait_for(
                    capture.stop.wait(),
                    self.capture_local.remaining(
                        deadline,
                        self.capture_local.DURATION +
                        self.capture_local.MAX_SETTLE_SECONDS + 1.0))
            except TimeoutError:
                pass
            if capture.invalid_status:
                raise self.capture_local.CaptureFailure(
                    "The controller reported an invalid audio packet.")
            if capture.disconnect:
                raise self.capture_local.CaptureFailure(
                    "The headset disconnected during the capture.")
            if not capture.settled:
                raise self.capture_local.CaptureFailure(
                    "The headset did not stream audio.")
            if not capture.data:
                raise self.capture_local.CaptureFailure(
                    "The capture received no audio data.")
            return self.capture_local.CaptureReport(
                pcm=bytes(capture.data),
                settle_seconds=capture.settle_seconds,
                settle_after_sco_seconds=capture.settle_after_sco_seconds,
                silence_used=capture.silence_used,
                silence_error=capture.silence_error,
            )
        finally:
            self._capture = None
            capture.stop.set()
            for task in (settle_task, silence_task):
                if task is not None:
                    task.cancel()
            for task in (settle_task, silence_task):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
            for link in links:
                link.sink = None
                await self.capture_local.quiet(link.disconnect)
            await self._end_call()

    async def _drop_link(self):
        client = self.rfcomm_client
        self.rfcomm_client = None
        self.ag = None
        self.acl_alive = False
        if client is not None:
            await self.capture_local.quiet(client.shutdown)
        acl = self.acl
        self.acl = None
        if acl is not None:
            await self.capture_local.quiet(acl.disconnect)

    async def close(self):
        if self._capture is not None:
            await self._cleanup_capture(self._capture)
        await self._drop_link()
        if self.device is not None:
            await self.capture_local.quiet(self.device.power_off)
            self.device = None
        if self.transport is not None:
            await self.capture_local.quiet(self.transport.close)
            self.transport = None
        try:
            released = await self.capture_local.wait_for_uart_release()
        except Exception:
            released = False
        if not released:
            print("A process still holds the UART after the session.",
                  file=sys.stderr, flush=True)


async def run_session(session):
    try:
        await session.run()
    finally:
        try:
            try:
                session.prepare_cleanup()
            except Exception:
                pass
            session.experiment.cleanup()
        finally:
            try:
                session.remove_state_file()
                session.remove_command_file()
            except Exception:
                pass
            session.lock.release()


def client_main(args):
    command = args.client
    if not hasattr(socket, "AF_UNIX"):
        print("error This platform has no Unix domain sockets.", flush=True)
        return 1
    if args.timeout is not None:
        timeout = args.timeout
    elif command in ("record", "start_recording", "stop_recording"):
        timeout = RECORD_CLIENT_TIMEOUT
    else:
        timeout = CONTROL_TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(args.socket)
            sock.sendall(command.encode("ascii") + b"\n")
            data = b""
            while b"\n" not in data and len(data) < CONTROL_LINE_LIMIT:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError,
            BrokenPipeError):
        print("error The Bluetooth session is not running.", flush=True)
        return 1
    except TimeoutError:
        print("error The Bluetooth session did not reply.", flush=True)
        return 1
    except OSError as error:
        reason = error.strerror or "unknown error"
        print(f"error The control socket failed: {reason}.", flush=True)
        return 1
    line = data.split(b"\n", 1)[0].decode("ascii", "replace").strip()
    if not line:
        print("error The Bluetooth session sent no reply.", flush=True)
        return 1
    print(line, flush=True)
    return 0 if line.startswith("ok ") else 1


def build_parser():
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Keep a Bluetooth session open for repeated headset recordings.",
        epilog=(
            "Manual start only. The session stops the stock Bluetooth daemon once. "
            "It restores Bluetooth when you stop it, after the idle time limit, or on an error. "
            "Restoration is observed, not guaranteed. "
            "The link key stays in memory. "
            "The recording may contain private speech."
        ),
    )
    parser.add_argument("--client", choices=CLIENT_COMMANDS, metavar="COMMAND",
                        help="Send one command to a running session and print the reply. "
                             "Commands: record, start_recording, stop_recording, "
                             "status, phase, state, result, stop.")
    parser.add_argument("--socket", default=SOCKET_PATH, metavar="PATH",
                        help="Control socket path. The default is " + SOCKET_PATH + ".")
    parser.add_argument("--timeout", type=positive_float, metavar="SECONDS",
                        help="Client reply timeout. The default is 5 seconds for status "
                             "and stop, and 200 seconds for recording commands.")
    parser.add_argument("--config", metavar="FILE",
                        help="Upload configuration with server_url and token. "
                             "The session keeps the file so later sessions can reuse it.")
    parser.add_argument("--headset", type=headset_address, metavar="ADDRESS",
                        help="Headset Bluetooth address. Omit when the pairing store "
                             "holds one device.")
    parser.add_argument("--idle-timeout", type=positive_int, default=DEFAULT_IDLE_TIMEOUT,
                        metavar="SECONDS",
                        help="Stop after this many seconds without a recording. "
                             "The default is 1800.")
    parser.add_argument("--keep-latency", action="store_true",
                        help="Keep the CPU latency hold for the whole session. "
                             "The default releases it between recordings.")
    parser.add_argument("--send-silence", action="store_true",
                        help="Send paced silent audio to the headset during each capture. "
                             "This may make the headset start its microphone sooner. "
                             "The default is off.")
    parser.add_argument("--authorize-bluetooth-handoff", action="store_true",
                        help="Confirm that you approve the Bluetooth handoff.")
    parser.add_argument("--authorize-microphone-recording", action="store_true",
                        help="Confirm that you approve microphone recording.")
    parser.add_argument("--confirm-bluetooth-enabled-in-ui", action="store_true",
                        help="Confirm that the Kindle UI shows Bluetooth enabled.")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.client:
        return client_main(args)
    if not args.config:
        parser.error("--config is required unless --client is used.")
    for name in ("authorize_bluetooth_handoff", "authorize_microphone_recording",
                 "confirm_bluetooth_enabled_in_ui"):
        if not getattr(args, name):
            parser.error("--" + name.replace("_", "-") + " is required.")
    if not os.path.isabs(args.socket):
        parser.error("--socket must be an absolute path.")

    for line in WARNINGS:
        print(line, flush=True)
    print("", flush=True)

    try:
        capture_local, pw4_supervisor, uploader = load_modules()
        config = capture_local.read_config(args.config)
        keystore, address = capture_local.load_keystore(
            capture_local.PAIRING_STORE, args.headset)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        return 2

    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass

    print(f"Headset: {address}", flush=True)
    print("The link key was read from the pairing store. It stays in memory.", flush=True)
    print(f"The control socket is {args.socket}.", flush=True)
    print(f"The idle time limit is {args.idle_timeout} seconds.", flush=True)
    if args.keep_latency:
        print("The CPU latency hold stays for the whole session.", flush=True)
    else:
        print("The CPU latency hold is released between recordings.", flush=True)
    print(f"Downlink silence: {'on' if args.send_silence else 'off'}.", flush=True)

    def raise_interrupt(signum, frame):
        raise pw4_supervisor.Interrupted(f"Signal {signum} stopped the session.")

    experiment = pw4_supervisor.Experiment()
    session = Session(
        experiment=experiment,
        controller_factory=lambda: BumbleController(
            capture_local, keystore, address, send_silence=args.send_silence),
        config=config,
        socket_path=args.socket,
        lock_path=args.socket + ".lock",
        idle_timeout=args.idle_timeout,
        handoff_budget=max(210.0, args.idle_timeout + CLEANUP_RESERVE_SECONDS),
        upload_timeout=capture_local.UPLOAD_TIMEOUT,
        capture_local=capture_local,
        uploader=uploader,
        pw4_supervisor=pw4_supervisor,
        keep_latency=args.keep_latency,
    )

    old_handlers = {}
    failed = False
    try:
        signals = [signal.SIGTERM, signal.SIGINT]
        if hasattr(signal, "SIGALRM"):
            signals.append(signal.SIGALRM)
        for sig in signals:
            old_handlers[sig] = signal.signal(sig, raise_interrupt)
        if hasattr(signal, "SIGALRM"):
            signal.alarm(STARTUP_SECONDS)
        asyncio.run(run_session(session))
    except pw4_supervisor.Interrupted as error:
        failed = True
        print(f"Stopped: {error}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        failed = True
        print("Stopped.", file=sys.stderr, flush=True)
    except Exception as error:
        failed = True
        print(f"The session stopped on an unexpected error: {type(error).__name__}.",
              file=sys.stderr, flush=True)
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    if experiment.cleanup_failed:
        print("Cleanup failed. Check Bluetooth on the device.", file=sys.stderr, flush=True)
    return 1 if failed or experiment.cleanup_failed else 0


if __name__ == "__main__":
    sys.exit(main())
