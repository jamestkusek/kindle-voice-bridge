#!/usr/bin/env python3
"""Start and stop Bumble against an authorized HCI relay."""

import argparse
import asyncio
import errno
import ipaddress
import logging
import os
import socket
import sys

try:
    import bumble.transport
    from bumble.device import Device, DeviceConfiguration
except Exception:
    bumble = None
    Device = None
    DeviceConfiguration = None


STARTUP_TIMEOUT = 60.0
CLIENT_WAIT_TIMEOUT = 30.0
CLIENT_POLL_INTERVAL = 0.05
CLEANUP_TIMEOUT = 3.0
SAFETY_NOTICE = (
    "Bumble power_off does not restore the Kindle's stock Bluetooth.\n"
    "Do not test HCI until you review recovery and explicitly authorize the handoff."
)


class ServerShapeError(Exception):
    pass


class ClientDisconnected(Exception):
    pass


class ProbeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Do not echo argument values, which can contain private data.
        print(SAFETY_NOTICE, file=sys.stderr)
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


def make_parser() -> argparse.ArgumentParser:
    parser = ProbeParser(
        allow_abbrev=False,
        description="Listen for one Kindle HCI relay and start Bumble.",
        epilog=(
            "Bumble power_on resets the controller and changes its configuration. "
            "The listener does not authenticate clients. "
            "The relay carries sensitive HCI data over unencrypted TCP. "
            "Bumble power_off does not restore the Kindle's stock Bluetooth. "
            "Do not test HCI until you review recovery and explicitly authorize the handoff. "
            "This probe does not pair, scan, capture audio, or change system services."
        ),
    )
    parser.add_argument(
        "--host", type=ipv4_address, metavar="IP",
        help="Specific laptop Wi-Fi IPv4 address for the listener.",
    )
    parser.add_argument("--port", type=port_number, metavar="PORT", help="Listener TCP port.")
    parser.add_argument(
        "--acknowledge-controller-reset",
        action="store_true",
        help="Acknowledge the controller reset and configuration changes.",
    )
    parser.add_argument(
        "--acknowledge-unencrypted-hci",
        action="store_true",
        help="Acknowledge that unencrypted TCP carries sensitive HCI data.",
    )
    parser.add_argument(
        "--authorize-handoff",
        action="store_true",
        help="Confirm that you reviewed recovery and explicitly authorize the handoff.",
    )
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


def transport_error_details(error: Exception, include_type: bool = False) -> str:
    details = []
    if include_type:
        name = type(error).__name__
        if not name.isascii() or not name.isidentifier() or len(name) > 80:
            name = "unknown"
        details.append(f"type={name}")
    for field in ("errno", "winerror"):
        value = getattr(error, field, None)
        if type(value) is int:
            details.append(f"{field}={value}")
    return f" ({', '.join(details)})" if details else ""


def server_parts(transport):
    try:
        source, sink, server = transport.source, transport.sink, transport.server
        if not all((
            callable(source.set_packet_sink), callable(sink.on_packet),
            callable(server.close), callable(server.wait_closed),
            callable(transport.close), hasattr(sink, "transport"),
        )):
            raise ServerShapeError()
    except AttributeError as error:
        raise ServerShapeError() from error
    return sink, server


async def wait_for_client(sink):
    async with asyncio.timeout(CLIENT_WAIT_TIMEOUT):
        while True:
            try:
                client = sink.transport
            except AttributeError as error:
                raise ServerShapeError() from error
            if client is not None:
                if not all(callable(getattr(client, name, None)) for name in (
                    "write", "close", "is_closing",
                )):
                    raise ServerShapeError()
                if client.is_closing():
                    raise ClientDisconnected()
                return client
            await asyncio.sleep(CLIENT_POLL_INTERVAL)


async def run_probe(host: str, port: int) -> tuple[str, bool]:
    transport = None
    device = None
    client = None
    phase = "transport connection"
    result = "HCI startup failed."
    cleanup_ok = True
    try:
        ipv4_address(host)
        async with asyncio.timeout(STARTUP_TIMEOUT):
            transport = await bumble.transport.open_transport(f"tcp-server:{host}:{port}")
            sink, server = server_parts(transport)
            phase = "client wait"
            print("The HCI listener is ready. The probe waits for a client.", flush=True)
            client = await wait_for_client(sink)
            server.close()
            if sink.transport is not client or client.is_closing():
                raise ClientDisconnected()
            phase = "controller setup"
            config = DeviceConfiguration(
                name="Voice Bridge Probe",
                classic_enabled=True,
                le_enabled=False,
                connectable=False,
                discoverable=False,
                classic_accept_any=False,
                classic_interlaced_scan_enabled=False,
                classic_smp_enabled=False,
                gap_service_enabled=False,
                gatt_service_enabled=False,
            )
            device = Device.from_config_with_hci(config, transport.source, transport.sink)
            await device.power_on()
            result = "HCI startup succeeded."
    except ServerShapeError:
        result = "HCI local setup failed: unsupported Bumble server transport."
    except ClientDisconnected:
        result = "HCI client disconnected before controller setup."
    except argparse.ArgumentTypeError:
        result = "HCI local setup failed: invalid listener address."
    except TimeoutError as error:
        details = transport_error_details(error) if phase == "transport connection" else ""
        result = f"HCI {phase} timed out{details}."
    except Exception as error:
        # Bumble errors can contain addresses or HCI data.
        if phase == "transport connection":
            kind = connection_error_kind(error)
            details = transport_error_details(
                error, include_type=kind in ("operating system error", "unexpected error")
            )
            result = f"HCI {phase} failed: {kind}{details}."
        elif isinstance(error, OSError):
            result = f"HCI {phase} failed: {connection_error_kind(error)}."
        else:
            result = f"HCI {phase} failed."
    finally:
        server = getattr(transport, "server", None)
        if transport is not None:
            try:
                server.close()
            except Exception:
                cleanup_ok = False
        if device is not None:
            try:
                await asyncio.wait_for(device.power_off(), CLEANUP_TIMEOUT)
            except Exception:
                cleanup_ok = False
        if transport is not None:
            try:
                if client is None:
                    client = getattr(getattr(transport, "sink", None), "transport", None)
                if client is not None:
                    client.close()
            except Exception:
                cleanup_ok = False
            try:
                await asyncio.wait_for(transport.close(), CLEANUP_TIMEOUT)
            except Exception:
                cleanup_ok = False
            try:
                await asyncio.wait_for(server.wait_closed(), CLEANUP_TIMEOUT)
            except Exception:
                cleanup_ok = False
    return result, cleanup_ok


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    print(SAFETY_NOTICE, file=sys.stderr)
    if not (
        args.acknowledge_controller_reset
        and args.acknowledge_unencrypted_hci
        and args.authorize_handoff
    ):
        print("Refused. Supply all three acknowledgement flags. See --help.", file=sys.stderr)
        return 2
    if args.host is None or args.port is None:
        print("Refused. Supply --host and --port. See --help.", file=sys.stderr)
        return 2
    if bumble is None:
        print("HCI local setup failed: Bumble could not load.", file=sys.stderr)
        return 1

    # Bumble can record raw HCI data when this environment variable is set.
    snooper = os.environ.pop("BUMBLE_SNOOPER", None)
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        try:
            result, cleanup_ok = asyncio.run(run_probe(args.host, args.port))
        except KeyboardInterrupt:
            print("HCI probe interrupted. Cleanup was attempted.", file=sys.stderr)
            return 130
        except Exception:
            print("HCI probe failed.", file=sys.stderr)
            return 1
    finally:
        logging.disable(previous_disable)
        if snooper is not None:
            os.environ["BUMBLE_SNOOPER"] = snooper

    print(result)
    if not cleanup_ok:
        print("Cleanup failed or timed out.", file=sys.stderr)
        return 1
    return 0 if result == "HCI startup succeeded." else 1


if __name__ == "__main__":
    sys.exit(main())
