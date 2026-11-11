"""Hold one TCP connection to the laptop for the length of a session.

This is a measurement, not a feature. It answers one question: does an open
Wi-Fi connection during SCO capture disturb the audio?

The daemon starts it only when ``link-test.json`` sits next to ``bt_session.py``.
The file holds ``host``, ``port`` and ``seconds``. Delete the file to disable the
probe.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

CONFIG_NAME = "link-test.json"
PING_INTERVAL_SECONDS = 1.0
CONNECT_TIMEOUT_SECONDS = 10.0


def config_path():
    """Return the path of the probe configuration next to this module."""

    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def load_config(path=None):
    """Return the probe settings, or ``None`` when the probe is off."""

    try:
        with open(path or config_path(), "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    host = data.get("host")
    port = data.get("port")
    seconds = data.get("seconds", 60)
    if not isinstance(host, str) or not host:
        return None
    if not isinstance(port, int) or not 0 < port < 65536:
        return None
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return {"host": host, "port": port, "seconds": float(seconds)}


async def hold_link(host, port, seconds, report=print):
    """Connect once, keep the socket open, and count what crosses it."""

    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as error:
        report(f"Link probe: connect failed: {type(error).__name__}.")
        return

    report(f"Link probe: connected to {host}:{port} for {seconds:g} s.")
    sent = 0
    received = 0

    async def read_loop():
        nonlocal received
        while True:
            line = await reader.readline()
            if not line:
                return
            received += 1

    read_task = asyncio.create_task(read_loop())
    try:
        while time.monotonic() - started < seconds:
            writer.write(b"ping\n")
            await writer.drain()
            sent += 1
            await asyncio.sleep(PING_INTERVAL_SECONDS)
    except Exception as error:
        report(f"Link probe: stopped: {type(error).__name__}.")
    finally:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    held = time.monotonic() - started
    report(f"Link probe: held {held:.1f} s, sent {sent}, received {received}.")
