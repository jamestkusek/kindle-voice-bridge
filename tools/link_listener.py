#!/usr/bin/env python3
"""Hold one TCP connection open for the Kindle Wi-Fi probe.

This is the laptop half of the measurement in
``kindle/voice_bridge/link_probe.py``. It accepts one connection, sends a short
line every second, and counts the lines the Kindle sends back. It is not part of
the product.

The Kindle blocks inbound TCP, so the Kindle connects out and this program
listens. Use a trusted network only.
"""

import argparse
import socket
import sys
import time

DEFAULT_PORT = 18770
DEFAULT_SECONDS = 120.0
SEND_INTERVAL_SECONDS = 1.0


def serve(host, port, seconds, report=print):
    """Accept one client, exchange lines, and return the counts."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(1)
    listener.settimeout(seconds)
    report(f"Listening on {host}:{port} for {seconds:g} s.")

    try:
        client, address = listener.accept()
    except socket.timeout:
        report("No client connected before the deadline.")
        listener.close()
        return 0, 0
    finally:
        pass

    report(f"Client connected from {address[0]}.")
    client.settimeout(0.5)
    started = time.monotonic()
    sent = 0
    received = 0
    buffer = b""
    next_send = started

    try:
        while time.monotonic() - started < seconds:
            now = time.monotonic()
            if now >= next_send:
                client.sendall(f"tick {sent}\n".encode("ascii"))
                sent += 1
                next_send = now + SEND_INTERVAL_SECONDS
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                report("The client closed the connection.")
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                received += 1
                if received <= 3:
                    report(f"  received: {line.decode('ascii', 'replace')}")
    except OSError as error:
        report(f"The connection failed: {error}.")
    finally:
        client.close()
        listener.close()

    held = time.monotonic() - started
    report(f"Held {held:.1f} s, sent {sent}, received {received}.")
    return sent, received


def main(argv=None):
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Hold one TCP connection open for the Kindle Wi-Fi probe.",
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="address to listen on. The default is 0.0.0.0.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"port to listen on. The default is {DEFAULT_PORT}.")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help=f"how long to hold the connection. "
                             f"The default is {DEFAULT_SECONDS:g}.")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
