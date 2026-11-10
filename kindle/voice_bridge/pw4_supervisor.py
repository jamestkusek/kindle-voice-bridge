#!/usr/bin/env python3
"""Manually relay one raw H:4 connection after a PW4 Bluetooth handoff.

The handoff and the restore are separate methods, so another manual script can
reuse them without a laptop relay. prepare(), handoff() and open_uart() take the
UART from bsa_server. cleanup() restores Bluetooth and must always run.
"""

import argparse
import os
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import time

if __package__:
    from . import hci_relay
else:
    import hci_relay

UART = "/dev/ttymxc2"
CPU_DMA_LATENCY = "/dev/cpu_dma_latency"
PROTO = "/proc/bluetooth/sleep/proto"
BTWAKE = "/proc/bluetooth/sleep/btwake"
BTENABLE = "/proc/bluetooth/btenable"


class Interrupted(Exception):
    pass


def proc_stat(pid):
    with open(f"/proc/{pid}/stat", encoding="ascii") as source:
        data = source.read()
    # The command name can contain spaces and parentheses.
    end = data.rfind(")")
    if end < 0 or data[end:end + 2] != ") ":
        raise RuntimeError(f"Cannot read process identity for PID {pid}.")
    fields = data[end + 2:].split()
    if len(fields) < 20:
        raise RuntimeError(f"Cannot read process identity for PID {pid}.")
    return fields[0], int(fields[19])


def processes():
    """Return visible process identities and UART holders."""
    device = os.stat(UART)
    if not stat.S_ISCHR(device.st_mode):
        raise RuntimeError("The UART path is not a character device.")
    names = {"btd": [], "bsa_server": [], "btfd": []}
    holders = set()
    for entry in os.listdir("/proc"):
        if not entry.isdecimal():
            continue
        pid = int(entry)
        try:
            state, start = proc_stat(pid)
            with open(f"/proc/{pid}/comm", encoding="utf-8") as source:
                name = source.read().rstrip("\n")
            if name in names:
                exe = os.readlink(f"/proc/{pid}/exe")
                if os.path.basename(exe) != name:
                    raise RuntimeError(f"The executable for {name} is ambiguous.")
                names[name].append((pid, start, state, exe))
            directory = f"/proc/{pid}/fd"
            for fd in os.listdir(directory):
                try:
                    target = os.stat(f"{directory}/{fd}")
                except FileNotFoundError:
                    # A descriptor can close during the scan.
                    continue
                if stat.S_ISCHR(target.st_mode) and target.st_rdev == device.st_rdev:
                    holders.add(pid)
            if proc_stat(pid)[1] != start:
                raise RuntimeError(f"PID {pid} changed during the scan.")
        except FileNotFoundError:
            if os.path.exists(f"/proc/{pid}"):
                raise RuntimeError(f"Cannot inspect PID {pid}.")
        except PermissionError as error:
            raise RuntimeError(f"Cannot inspect PID {pid}.") from error
    return names, holders


def snapshot():
    names, holders = processes()
    for name in names:
        if len(names[name]) != 1 or names[name][0][2] in "TtXZ":
            raise RuntimeError(f"The running {name} identity is ambiguous.")
    btd, bsa, btfd = (names[name][0] for name in ("btd", "bsa_server", "btfd"))
    if holders != {bsa[0]}:
        raise RuntimeError("The UART holder is not uniquely bsa_server.")
    return btd, bsa, btfd


def same_process(expected, allow_stopped=False):
    pid, start, _state, exe = expected
    state, current_start = proc_stat(pid)
    with open(f"/proc/{pid}/comm", encoding="utf-8") as source:
        name = source.read().rstrip("\n")
    current_exe = os.readlink(f"/proc/{pid}/exe")
    final_state, final_start = proc_stat(pid)
    if (current_start != start or final_start != start or current_exe != exe or
            name != os.path.basename(exe) or state in "XZ" or final_state in "XZ"):
        raise RuntimeError(f"The identity of PID {pid} changed.")
    if not allow_stopped and final_state in "Tt":
        raise RuntimeError(f"PID {pid} is stopped.")
    return final_state


def identity(process):
    return process[0], process[1], process[3]


def target_signal(expected, sig, allow_stopped=False):
    # The identity check and the signal are not atomic. PID reuse can occur.
    same_process(expected, allow_stopped)
    os.kill(expected[0], sig)


def check_peer(client):
    # poll does not consume H:4 data. The peer can disconnect after this check.
    errors = (getattr(select, "POLLRDHUP", 0x2000) | select.POLLHUP |
              select.POLLERR | select.POLLNVAL)
    poller = select.poll()
    poller.register(client, errors)
    if any(events & errors for _fd, events in poller.poll(0)):
        raise ConnectionError("The laptop connection closed or failed.")


def wait_for(predicate, deadline, message):
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError(message)


def read_power():
    with open(BTENABLE, "rb") as source:
        value = source.read(129)
    if value not in (b"1", b"1\n"):
        raise RuntimeError("The Bluetooth power baseline is not exactly 1.")
    return value


def write_zero(path):
    with open(path, "w", encoding="ascii") as target:
        if target.write("0") != 1:
            raise OSError(f"Short write to {path}.")


def configure_uart(fd):
    settings = termios.tcgetattr(fd)
    new = [*settings]
    new[6] = settings[6][:]
    new[0] = termios.IGNBRK
    new[1] = 0
    new[2] = (new[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB))
    new[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL | termios.CRTSCTS
    new[3] = 0
    new[4] = new[5] = termios.B2000000
    new[6][termios.VMIN] = 0
    new[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, new)
    actual = termios.tcgetattr(fd)
    mask = (termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CREAD |
            termios.CLOCAL | termios.CRTSCTS)
    if (actual[0] != termios.IGNBRK or actual[1] != 0 or actual[3] != 0 or
            actual[2] & mask != new[2] & mask or
            actual[4:6] != new[4:6] or
            actual[6][termios.VMIN] != new[6][termios.VMIN] or
            actual[6][termios.VTIME] != new[6][termios.VTIME]):
        raise RuntimeError("The UART settings did not match the request.")


class Experiment:
    def __init__(self, args=None):
        self.args = args
        self.client = None
        self.uart = None
        self.uart_closed = False
        self.latency_fd = None
        self.saved = None
        self.btd = None
        self.bsa = None
        self.stopped = False
        self.killed = False
        self.deadline = None
        self.commands = None
        self.cleanup_failed = False
        self.cleanup_timed_out = False
        self.pending = None

    def interrupt(self, signum, frame):
        self.pending = signum
        # Raise only inside the experiment. Cleanup must resume btd first.
        while frame is not None:
            if frame.f_code in (Experiment.prepare.__code__,
                                Experiment.handoff.__code__,
                                Experiment.open_uart.__code__,
                                Experiment.run.__code__):
                raise Interrupted(f"Signal {signum} stopped the experiment.")
            frame = frame.f_back

    def check_client(self):
        # The relay path has a client. A local caller has none.
        if self.client is not None:
            check_peer(self.client)

    def prepare(self):
        """Check the environment and record the Bluetooth process baseline."""
        if self.pending is not None:
            raise Interrupted(f"Signal {self.pending} stopped the experiment.")
        if any(not hasattr(termios, name) for name in ("B2000000", "CRTSCTS", "IGNBRK")):
            raise RuntimeError("The required UART settings are unavailable.")
        self.commands = [shutil.which(name) for name in ("initctl", "lipc-set-prop")]
        if any(command is None for command in self.commands):
            raise RuntimeError("initctl or lipc-set-prop is unavailable.")
        for path in (PROTO, BTWAKE, BTENABLE):
            if not os.path.isfile(path) or not os.access(path, os.W_OK):
                raise RuntimeError(f"The control {path} is not writable.")
        read_power()
        self.btd, self.bsa, self.btfd = snapshot()

    def handoff(self, budget=210.0):
        """Stop btd and take the UART from bsa_server. Call cleanup() after."""
        self.deadline = time.monotonic() + budget
        read_power()
        current = snapshot()
        if tuple(map(identity, current)) != tuple(
                map(identity, (self.btd, self.bsa, self.btfd))):
            raise RuntimeError("Bluetooth process identities changed before handoff.")
        blocked = {signal.SIGALRM, signal.SIGTERM, signal.SIGINT}
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            self.latency_fd = os.open(CPU_DMA_LATENCY, os.O_WRONLY)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        request = struct.pack("=i", 0)
        if os.write(self.latency_fd, request) != len(request):
            raise OSError("The CPU latency request was not fully written.")
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            self.check_client()
            target_signal(self.btd, signal.SIGSTOP)
            self.stopped = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        wait_for(lambda: same_process(self.btd, True) in "Tt", time.monotonic() + 2,
                 "btd did not stop.")
        names, holders = processes()
        if (holders != {self.bsa[0]} or len(names["bsa_server"]) != 1 or
                identity(names["bsa_server"][0]) != identity(self.bsa) or
                len(names["btd"]) != 1 or identity(names["btd"][0]) != identity(self.btd) or
                len(names["btfd"]) != 1 or
                identity(names["btfd"][0]) != identity(self.btfd) or
                names["btfd"][0][2] in "TtXZ" or same_process(self.btd, True) not in "Tt"):
            raise RuntimeError("The Bluetooth processes changed before handoff.")
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        try:
            self.check_client()
            target_signal(self.bsa, signal.SIGKILL)
            self.killed = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        self.check_client()
        wait_for(lambda: not processes()[1], time.monotonic() + 2,
                 "The UART was not released.")
        write_zero(PROTO)
        write_zero(BTWAKE)
        time.sleep(0.5)
        if processes()[1]:
            raise RuntimeError("A process reclaimed the UART.")

    def open_uart(self, defer=False):
        """Open the UART and apply the raw H:4 settings.

        With defer, close the descriptor again and keep the recorded settings,
        so the caller owns the port and cleanup() can restore the settings.
        """
        self.uart = os.open(UART, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        if processes()[1] != {os.getpid()}:
            raise RuntimeError("The UART is not held exclusively by this process.")
        self.saved = termios.tcgetattr(self.uart)
        configure_uart(self.uart)
        if defer:
            os.close(self.uart)
            self.uart = None
            self.uart_closed = True

    def run(self):
        self.prepare()
        # The connection attempt has its own limit. No handoff occurs before it.
        signal.alarm(180)
        print("Connecting to the laptop.", flush=True)
        self.client = hci_relay.connect_to_laptop(
            self.args.bind, self.args.peer, self.args.port, 20)
        print("Connected to the laptop.", flush=True)
        # Reserve twenty seconds for cleanup.
        signal.alarm(190)
        self.handoff()
        self.open_uart()
        hci_relay.relay(self.client, self.uart)
        print("The client disconnected.", flush=True)

    def cleanup_interrupt(self, signum, frame):
        self.cleanup_failed = True
        self.cleanup_timed_out = True
        while frame is not None:
            if frame.f_code is Experiment.attempt.__code__:
                raise Interrupted("The cleanup time limit expired.")
            frame = frame.f_back

    def restore_uart_settings(self):
        """Reopen the UART and restore the settings recorded before handoff."""
        fd = os.open(UART, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            termios.tcsetattr(fd, termios.TCSANOW, self.saved)
        finally:
            os.close(fd)

    def attempt(self, label, action):
        try:
            print(f"Cleanup: {label}.", flush=True)
            action()
        except Exception as error:
            self.cleanup_failed = True
            print(f"Cleanup failed: {label}: {error}", file=sys.stderr, flush=True)

    def cleanup(self):
        blocked = {signal.SIGALRM, signal.SIGTERM, signal.SIGINT}
        # Ignore pending signals until the SIGCONT attempt is complete.
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
        for sig in blocked:
            signal.signal(sig, signal.SIG_IGN)
        signal.alarm(0)
        if self.uart is not None and self.saved is not None:
            self.attempt("restore UART settings", lambda: termios.tcsetattr(
                self.uart, termios.TCSANOW, self.saved))
        if self.uart is not None:
            self.attempt("close UART", lambda: os.close(self.uart))
        if self.uart_closed and self.saved is not None:
            self.attempt("restore UART settings after handover",
                         self.restore_uart_settings)
        if self.latency_fd is not None:
            self.attempt("close CPU latency request", lambda: os.close(self.latency_fd))
        if self.client is not None:
            self.attempt("close client", self.client.close)
        if self.stopped:
            self.attempt("resume btd", lambda: target_signal(self.btd, signal.SIGCONT, True))
        if self.deadline is not None:
            remaining = int(self.deadline - time.monotonic())
            if remaining > 0:
                signal.signal(signal.SIGALRM, self.cleanup_interrupt)
                signal.alarm(remaining)
            else:
                self.cleanup_failed = self.cleanup_timed_out = True
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

        def available_time():
            remaining = self.deadline - time.monotonic()
            if self.cleanup_timed_out or remaining <= 0:
                raise RuntimeError("No time remains for Bluetooth recovery.")
            return remaining

        if self.killed:
            def disable():
                available_time()
                write_zero(BTENABLE)

            def request(command, label):
                subprocess.run(command, timeout=min(3, available_time()), check=True)
                print(label, flush=True)

            self.attempt("disable Bluetooth power", disable)
            self.attempt("restart btd", lambda: request(
                [self.commands[0], "restart", "btd"], "The btd restart command succeeded."))
            self.attempt("request Bluetooth on", lambda: request(
                [self.commands[1], "com.lab126.btfd", "BTenable", "1:1"],
                "The Bluetooth-on request succeeded."))

        if self.stopped or self.killed:
            def observe():
                available_time()
                observed = {}
                def ready():
                    names, holders = processes()
                    observed["btd"] = (len(names["btd"]) == 1 and
                                       names["btd"][0][2] not in "TtXZ")
                    observed["bsa"] = (len(names["bsa_server"]) == 1 and
                                       names["bsa_server"][0][2] not in "TtXZ" and
                                       holders == {names["bsa_server"][0][0]})
                    return observed["btd"] and observed["bsa"]
                try:
                    wait_for(ready, min(self.deadline, time.monotonic() + 5),
                             "The expected Bluetooth processes were not observed.")
                finally:
                    if observed:
                        print(f"btd running: {observed['btd']}. "
                              f"bsa_server holds UART: {observed['bsa']}.", flush=True)
            self.attempt("check Bluetooth processes", observe)
            print("Stock Bluetooth operation is not verified.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, type=hci_relay.ipv4_address)
    parser.add_argument("--peer", required=True, type=hci_relay.ipv4_address)
    parser.add_argument("--port", required=True, type=hci_relay.port_number)
    parser.add_argument("--authorize-bluetooth-handoff", required=True, action="store_true")
    parser.add_argument("--acknowledge-plaintext-hci", required=True, action="store_true")
    parser.add_argument("--confirm-bluetooth-enabled-in-ui", required=True, action="store_true",
                        help="Confirm that the Kindle UI shows Bluetooth enabled.")
    args = parser.parse_args(argv)

    print("The LAN source IP is not authentication. HCI is plaintext.", flush=True)
    print("A crash can leave Bluetooth unusable. D: access may disappear.", flush=True)
    print("Reboot is not guaranteed to restore Bluetooth.", flush=True)
    print("Cleanup does not prove that stock Bluetooth works.", flush=True)
    print("The watchdog cannot protect against SIGKILL or a hung kernel.", flush=True)
    print("The PID check and signal are not atomic. PID reuse can signal another process.", flush=True)

    experiment = Experiment(args)
    old_handlers = {}
    failed = False
    try:
        for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, experiment.interrupt)
        signal.alarm(60)
        experiment.run()
    except Exception as error:
        failed = True
        print(f"Experiment failed: {error}", file=sys.stderr, flush=True)
    finally:
        experiment.cleanup()
        signal.alarm(0)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    return 1 if failed or experiment.cleanup_failed or experiment.pending is not None else 0


if __name__ == "__main__":
    sys.exit(main())
