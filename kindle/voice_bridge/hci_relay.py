#!/usr/bin/env python3
"""Relay raw H:4 bytes between one TCP client and an existing UART."""

import argparse
import ipaddress
import os
import select
import socket
import sys


BUFFER_LIMIT = 65536
READ_SIZE = 4096


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


def relay(client: socket.socket, uart_fd: int) -> None:
    """Copy bytes until the client disconnects or an operation fails."""
    client.setblocking(False)
    to_uart = bytearray()
    to_client = bytearray()

    while True:
        readers = []
        writers = []
        if len(to_uart) < BUFFER_LIMIT:
            readers.append(client)
        if len(to_client) < BUFFER_LIMIT:
            readers.append(uart_fd)
        if to_client:
            writers.append(client)
        if to_uart:
            writers.append(uart_fd)

        try:
            ready_read, ready_write, _ = select.select(readers, writers, [])
        except InterruptedError:
            continue

        if client in ready_read:
            try:
                data = client.recv(min(READ_SIZE, BUFFER_LIMIT - len(to_uart)))
            except (BlockingIOError, InterruptedError):
                pass
            else:
                if not data:
                    return
                to_uart.extend(data)

        if uart_fd in ready_read:
            try:
                data = os.read(uart_fd, min(READ_SIZE, BUFFER_LIMIT - len(to_client)))
            except (BlockingIOError, InterruptedError):
                pass
            else:
                if not data:
                    raise OSError("UART returned end of file")
                to_client.extend(data)

        if client in ready_write:
            try:
                count = client.send(to_client)
            except (BlockingIOError, InterruptedError):
                pass
            else:
                if count == 0:
                    raise OSError("TCP write returned zero bytes")
                del to_client[:count]

        if uart_fd in ready_write:
            try:
                count = os.write(uart_fd, to_uart)
            except (BlockingIOError, InterruptedError):
                pass
            else:
                if count == 0:
                    raise OSError("UART write returned zero bytes")
                del to_uart[:count]


def connect_to_laptop(bind_ip: str, peer_ip: str, port: int,
                      timeout: float) -> socket.socket:
    """Connect to the laptop from the given local IPv4 address."""
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # The bind address makes the source address predictable.
        client.bind((bind_ip, 0))
        client.settimeout(timeout)
        client.connect((peer_ip, port))
        if client.getpeername()[0] != peer_ip:
            raise OSError("The connection does not use the expected peer.")
        client.settimeout(None)
        return client
    except BaseException:
        client.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Relay raw H:4 bytes between one TCP client and one UART.",
        epilog=(
            "The source address is not authentication. "
            "Raw TCP is not encrypted. "
            "Plaintext HCI can expose Bluetooth traffic and keys. "
            "The UART must already be exclusively handed over and correctly configured. "
            "The relay does not check UART ownership or configuration. "
            "The relay discards buffered bytes on disconnect or error. "
            "This relay has not been tested on PW4."
        ),
    )
    parser.add_argument("--serial", required=True, help="Path to the already configured UART.")
    parser.add_argument(
        "--bind", required=True, type=ipv4_address, metavar="IP",
        help="Specific local IPv4 address for the outbound connection.",
    )
    parser.add_argument(
        "--peer", required=True, type=ipv4_address, metavar="IP",
        help="Expected IPv4 address of the laptop.",
    )
    parser.add_argument("--port", required=True, type=port_number,
                        help="TCP port on the laptop.")
    parser.add_argument(
        "--acknowledge-plaintext-hci", required=True, action="store_true",
        help="Acknowledge that plaintext HCI can expose Bluetooth traffic and keys.",
    )
    args = parser.parse_args(argv)

    try:
        with connect_to_laptop(args.bind, args.peer, args.port, 20) as client:
            uart_fd = os.open(args.serial, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                relay(client, uart_fd)
            finally:
                os.close(uart_fd)
        print("Client disconnected.", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"Relay error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
