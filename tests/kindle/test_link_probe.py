"""Tests for the Wi-Fi probe configuration and connection.

The probe is a measurement, not a feature. These tests check that a bad
configuration cannot reach the daemon and that the connection counts what
crosses it.
"""

import asyncio
import json

from kindle.voice_bridge import link_probe


def write_config(tmp_path, payload):
    path = tmp_path / "link-test.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_missing_file_turns_the_probe_off(tmp_path):
    assert link_probe.load_config(tmp_path / "absent.json") is None


def test_invalid_json_turns_the_probe_off(tmp_path):
    path = tmp_path / "link-test.json"
    path.write_text("{not json", encoding="utf-8")
    assert link_probe.load_config(path) is None


def test_a_missing_host_turns_the_probe_off(tmp_path):
    path = write_config(tmp_path, {"port": 18770})
    assert link_probe.load_config(path) is None


def test_an_invalid_port_turns_the_probe_off(tmp_path):
    path = write_config(tmp_path, {"host": "127.0.0.1", "port": 0})
    assert link_probe.load_config(path) is None


def test_a_valid_file_returns_the_settings(tmp_path):
    path = write_config(tmp_path, {"host": "127.0.0.1", "port": 18770, "seconds": 30})
    settings = link_probe.load_config(path)
    assert settings == {"host": "127.0.0.1", "port": 18770, "seconds": 30.0}


def test_the_default_duration_is_used_when_absent(tmp_path):
    path = write_config(tmp_path, {"host": "127.0.0.1", "port": 18770})
    assert link_probe.load_config(path)["seconds"] == 60.0


def test_hold_link_counts_both_directions():
    async def scenario():
        received = []

        async def handler(reader, writer):
            while True:
                line = await reader.readline()
                if not line:
                    break
                received.append(line.strip())
                writer.write(b"tick\n")
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        lines = []
        await link_probe.hold_link(
            "127.0.0.1", port, 2.5, report=lines.append)
        server.close()
        await server.wait_closed()
        return lines, received

    lines, received = asyncio.run(scenario())

    assert any("connected" in line for line in lines)
    assert any("held" in line for line in lines)
    assert len(received) >= 2


def test_hold_link_reports_a_failed_connection():
    lines = []
    asyncio.run(link_probe.hold_link("127.0.0.1", 1, 1.0, report=lines.append))
    assert lines and "connect failed" in lines[0]
