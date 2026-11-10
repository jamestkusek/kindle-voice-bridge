import asyncio
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROBE_PATH = Path(__file__).resolve().parents[2] / "tools" / "hci_probe.py"
spec = importlib.util.spec_from_file_location("hci_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

FLAGS = [
    "--acknowledge-controller-reset",
    "--acknowledge-unencrypted-hci",
    "--authorize-handoff",
]
TARGET = ["--host", "192.0.2.1", "--port", "1234"]


class ProbeTests(unittest.TestCase):
    def test_startup_timeout_is_60_seconds(self):
        self.assertEqual(probe.STARTUP_TIMEOUT, 60.0)

    def run_main(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = probe.main(args)
        return result, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def server_transport(close):
        """Build a transport shaped like Bumble's TCP server transport.

        The probe listens, so the transport carries a source, a sink that gains
        a client, and a server object. The client is present from the start so
        the client wait returns immediately.
        """
        client = SimpleNamespace(
            write=lambda data: None,
            close=lambda: None,
            is_closing=lambda: False,
        )

        class Server:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        class Sink:
            def __init__(self):
                self.transport = client

            def on_packet(self, packet):
                pass

        class Source:
            def set_packet_sink(self, sink):
                pass

        return SimpleNamespace(
            source=Source(), sink=Sink(), server=Server(), close=close,
        )

    def test_refuses_without_all_acknowledgements(self):
        for missing in [None, *FLAGS]:
            with self.subTest(missing=missing):
                flags = [] if missing is None else [f for f in FLAGS if f != missing]
                with patch.object(probe.bumble.transport, "open_transport") as opener:
                    result, _, stderr = self.run_main(TARGET + flags)
                self.assertEqual(result, 2)
                opener.assert_not_called()
                self.assertIn("Refused", stderr)
                self.assertIn("does not restore", stderr)
                self.assertIn("review recovery", stderr)

    def test_startup_and_cleanup_use_fake_objects(self):
        events = []
        test = self

        class FakeDevice:
            async def power_on(self):
                events.append("power_on")

            async def power_off(self):
                events.append("power_off")

        async def close():
            events.append("close")

        transport = self.server_transport(close)
        source, sink = transport.source, transport.sink

        async def open_transport(specification):
            test.assertEqual(specification, "tcp-server:192.0.2.1:1234")
            test.assertNotIn("BUMBLE_SNOOPER", probe.os.environ)
            events.append("open")
            return transport

        def make_device(config, actual_source, actual_sink):
            test.assertEqual(config.name, "Voice Bridge Probe")
            test.assertTrue(config.classic_enabled)
            for field in (
                "le_enabled", "connectable", "discoverable", "classic_accept_any",
                "classic_interlaced_scan_enabled", "classic_smp_enabled",
                "gap_service_enabled", "gatt_service_enabled",
            ):
                test.assertFalse(getattr(config, field), field)
            test.assertIs(actual_source, source)
            test.assertIs(actual_sink, sink)
            events.append("device")
            return FakeDevice()

        with patch.dict(probe.os.environ, {"BUMBLE_SNOOPER": "file:private"}), \
             patch.object(probe.bumble.transport, "open_transport", open_transport), \
             patch.object(probe.Device, "from_config_with_hci", make_device):
            result, stdout, stderr = self.run_main(TARGET + FLAGS)
            self.assertEqual(probe.os.environ["BUMBLE_SNOOPER"], "file:private")
        self.assertEqual(result, 0)
        self.assertEqual(events, ["open", "device", "power_on", "power_off", "close"])
        self.assertIn("HCI startup succeeded.", stdout)
        self.assertNotIn("192.0.2.1", stdout + stderr)

    def test_failed_startup_cleans_up_without_error_details(self):
        for mode in ("exception", "timeout"):
            with self.subTest(mode=mode):
                events = []

                class FakeDevice:
                    async def power_on(self):
                        events.append("power_on")
                        if mode == "timeout":
                            await asyncio.sleep(1)
                        raise RuntimeError("private-address-or-key")

                    async def power_off(self):
                        events.append("power_off")

                async def close():
                    events.append("close")

                async def open_transport(specification):
                    events.append("open")
                    return self.server_transport(close)

                with patch.object(probe, "STARTUP_TIMEOUT", 0.01), \
                     patch.object(probe.bumble.transport, "open_transport", open_transport), \
                     patch.object(probe.Device, "from_config_with_hci", lambda *a: FakeDevice()):
                    result, stdout, stderr = self.run_main(TARGET + FLAGS)
                self.assertEqual(result, 1)
                self.assertEqual(events, ["open", "power_on", "power_off", "close"])
                self.assertNotIn("private-address-or-key", stdout + stderr)
                self.assertIn("timed out" if mode == "timeout" else "failed", stdout)

    def test_connection_timeout_does_not_create_device(self):
        async def open_transport(specification):
            await asyncio.sleep(1)

        with patch.object(probe, "STARTUP_TIMEOUT", 0.01), \
             patch.object(probe.bumble.transport, "open_transport", open_transport), \
             patch.object(probe.Device, "from_config_with_hci") as make_device:
            result, stdout, _ = self.run_main(TARGET + FLAGS)
        self.assertEqual(result, 1)
        self.assertIn("timed out", stdout)
        make_device.assert_not_called()

    def test_cleanup_failure_still_closes_transport(self):
        events = []

        class FakeDevice:
            async def power_on(self):
                pass

            async def power_off(self):
                events.append("power_off")
                await asyncio.sleep(1)

        async def close():
            events.append("close")

        async def open_transport(specification):
            return self.server_transport(close)

        with patch.object(probe, "CLEANUP_TIMEOUT", 0.01), \
             patch.object(probe.bumble.transport, "open_transport", open_transport), \
             patch.object(probe.Device, "from_config_with_hci", lambda *a: FakeDevice()):
            result, _, stderr = self.run_main(TARGET + FLAGS)
        self.assertEqual(result, 1)
        self.assertEqual(events, ["power_off", "close"])
        self.assertIn("Cleanup failed or timed out.", stderr)

    def test_invalid_arguments_do_not_echo_values(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            probe.main(["--host", "private-value", "--unexpected", "private-value"])
        self.assertEqual(error.exception.code, 2)
        self.assertNotIn("private-value", stderr.getvalue())
        self.assertIn("does not restore", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
