#!/usr/bin/env python3
"""Start or request a stop of the manual HCI supervisor."""

import argparse
import fcntl
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time


ROOT = "/mnt/us/kindle-voice-bridge"
PYTHON = ROOT + "/python31117/bin/python3.11"
SUPERVISOR = ROOT + "/pw4_supervisor.py"
CONFIG = ROOT + "/hci-test.json"
LOG = ROOT + "/hci-test.log"
STATE = "/tmp/voicebridge-hci-test-state.json"
LOCK = "/tmp/voicebridge-hci-test.lock"
PROC = "/proc"
READINESS_TIMEOUT = 10.0
READINESS_POLL = 0.1
READINESS_LINE = b"Connecting to the laptop."


class LauncherError(Exception):
    pass


def boot_id():
    with open(PROC + "/sys/kernel/random/boot_id", encoding="ascii") as f:
        value = f.read().strip()
    if not value:
        raise LauncherError("Cannot read the boot ID.")
    return value


def process_stat(pid):
    try:
        with open(f"{PROC}/{pid}/stat", encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except FileNotFoundError:
        return None
    # The process name can contain spaces and closing parentheses.
    end = stat.rfind(")")
    fields = stat[end + 1:].split() if end >= 0 else []
    if len(fields) < 20 or not fields[19].isdigit():
        raise LauncherError("Cannot read the process start time.")
    return fields[0], int(fields[19])


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (ValueError, TypeError) as exc:
        raise LauncherError("Cannot read the state file. Do not start another supervisor.") from exc
    if not isinstance(state, dict) or not isinstance(state.get("boot_id"), str):
        raise LauncherError("The state file is invalid. Do not start another supervisor.")
    if state.get("phase") == "starting":
        raise LauncherError("The previous start did not finish. Check the supervisor before removing the state file.")
    if (state.get("phase") != "running" or type(state.get("pid")) is not int
            or state["pid"] <= 0 or type(state.get("start_time")) is not int
            or state["start_time"] < 0):
        raise LauncherError("The state file is invalid. Do not start another supervisor.")
    return state


def save_state(state):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(STATE),
                                         prefix=".hci-state-", delete=False) as f:
            name = f.name
            json.dump(state, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, STATE)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def remove_state():
    try:
        os.unlink(STATE)
    except FileNotFoundError:
        pass


def supervisor_status(state):
    if state["boot_id"] != boot_id():
        return "exited"
    stat = process_stat(state["pid"])
    if stat is None or stat[0] in ("Z", "X") or stat[1] != state["start_time"]:
        return "exited"
    try:
        with open(f"{PROC}/{state['pid']}/cmdline", "rb") as f:
            args = f.read().split(b"\0")
    except FileNotFoundError:
        return "exited"
    stat_after = process_stat(state["pid"])
    if stat_after is None or stat_after[0] in ("Z", "X") or stat_after[1] != state["start_time"]:
        return "exited"
    if args[:3] != [os.fsencode(PYTHON), b"-B", os.fsencode(SUPERVISOR)]:
        raise LauncherError("The process does not match the supervisor. No signal was sent.")
    return "running"


def config_and_bind():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict) or not isinstance(config.get("laptop_ip"), str):
            raise ValueError
        peer = str(ipaddress.IPv4Address(config["laptop_ip"]))
        port = config.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise LauncherError("Set laptop_ip to a numeric IPv4 address and port to an integer from 1 to 65535 in hci-test.json.") from exc
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((peer, port))
        bind = sock.getsockname()[0]
    if bind == "0.0.0.0":
        raise LauncherError("Cannot determine the Kindle IPv4 address.")
    return peer, port, bind


def wait_for_readiness(process, log_reader):
    deadline = time.monotonic() + READINESS_TIMEOUT
    pending = b""
    while True:
        status = process.poll()
        if status is not None:
            raise LauncherError(f"The supervisor exited with status {status} before readiness. Do not connect. Check hci-test.log.")
        try:
            pending += log_reader.read()
        except OSError as exc:
            raise LauncherError("Cannot read supervisor readiness. Do not connect. The supervisor may still be running.") from exc
        lines = pending.split(b"\n")
        pending = lines.pop()
        if READINESS_LINE in lines:
            status = process.poll()
            if status is not None:
                raise LauncherError(f"The supervisor exited with status {status} before readiness. Do not connect. Check hci-test.log.")
            if time.monotonic() < deadline:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError(f"The supervisor did not report readiness within {READINESS_TIMEOUT:g} seconds. Do not connect. It may still be running. Use Stop HCI probe if needed. Check hci-test.log.")
        time.sleep(min(READINESS_POLL, remaining))


def start():
    state = read_state()
    if state is not None:
        if supervisor_status(state) == "running":
            raise LauncherError("The supervisor is already running. A second start is not allowed.")
        remove_state()
    peer, port, bind = config_and_bind()
    current_boot = boot_id()
    args = [PYTHON, "-B", SUPERVISOR, "--bind", bind, "--peer", peer,
            "--port", str(port), "--authorize-bluetooth-handoff",
            "--acknowledge-plaintext-hci", "--confirm-bluetooth-enabled-in-ui"]
    with open(LOG, "ab") as log, open(LOG, "rb") as log_reader:
        log_reader.seek(0, os.SEEK_END)
        if log_reader.tell() > 0:
            log_reader.seek(-1, os.SEEK_END)
            if log_reader.read(1) != b"\n":
                log.write(b"\n")
                log.flush()
        save_state({"phase": "starting", "boot_id": current_boot})
        try:
            log_reader.seek(0, os.SEEK_END)
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            remove_state()
            raise
        try:
            stat = process_stat(process.pid)
        except (OSError, LauncherError) as exc:
            raise LauncherError("Cannot record the supervisor. Do not connect. It may be running. Check it before removing the state file.") from exc
        if stat is None:
            raise LauncherError("Cannot record the supervisor. Do not connect. It may be running. Check it before removing the state file.")
        try:
            save_state({"phase": "running", "boot_id": current_boot,
                        "pid": process.pid, "start_time": stat[1]})
        except OSError as exc:
            raise LauncherError("Cannot record the supervisor. Do not connect. It may be running. Check it before removing the state file.") from exc
        wait_for_readiness(process, log_reader)
    print("Kindle IPv4: " + bind)
    print("The supervisor is connecting to the laptop.")


def stop():
    state = read_state()
    if state is None:
        print("No supervisor is recorded.")
        return
    if supervisor_status(state) == "exited":
        remove_state()
        print("The recorded supervisor exited. The stale state was removed.")
        return
    try:
        os.kill(state["pid"], signal.SIGTERM)
    except ProcessLookupError:
        remove_state()
        print("The recorded supervisor exited. The stale state was removed.")
        return
    except OSError as exc:
        raise LauncherError("Cannot send the stop request: " + str(exc)) from exc
    print("A stop was requested. The identity check and signal are not atomic. A reused PID could receive the signal.")
    print("Bluetooth restoration is not guaranteed. Check hci-test.log.")


def main():
    parser = argparse.ArgumentParser(description="Control the manual HCI supervisor.")
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--confirm-bluetooth-enabled-in-ui", action="store_true")
    args = parser.parse_args()
    if args.command == "start" and not args.confirm_bluetooth_enabled_in_ui:
        print("Confirm that Bluetooth is enabled in the Kindle UI. Use --confirm-bluetooth-enabled-in-ui to start.", file=sys.stderr)
        return 1
    if args.command == "stop" and args.confirm_bluetooth_enabled_in_ui:
        print("The Bluetooth confirmation is only for start.", file=sys.stderr)
        return 1
    try:
        with open(LOCK, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LauncherError("Another launcher command is running. Try again.") from exc
            if args.command == "start":
                start()
            else:
                stop()
    except LauncherError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("The launcher cannot access a required file or network route: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
