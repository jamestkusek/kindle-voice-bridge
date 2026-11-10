#!/usr/bin/env python3
"""Convert a Kindle zbluetooth bt_devices.xml export into a Bumble JsonKeyStore.

Confirmed PW4 shape: a <bt_devices> root holding <device> elements with
<bd_addr>, <link_key> and <key_type> children, for example
<bd_addr>95:05:BB:2A:34:2E:</bd_addr>. Values are colon-separated hex with a
trailing colon. Each peer is written under both its plain address and Bumble's
public form (AA:BB:CC:DD:EE:FF/P), because Bumble looks classic peers up as
public addresses. The tool prints Bluetooth addresses only and never prints key
material.
"""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

AUTHENTICATED_KEY_TYPES = (0x05, 0x08)
HEX = "0123456789abcdef"


def normalise(text, digits):
    """Return the hex digits of a colon-separated value, or None."""
    value = "".join(text.split()).replace(":", "").lower()
    if len(value) != digits or any(char not in HEX for char in value):
        return None
    return value


def parse(path):
    """Return {address: (key_hex, key_type)} for every usable device element."""
    devices = {}
    for element in ET.parse(path).getroot().iter("device"):
        fields = {child.tag.strip().lower(): (child.text or "") for child in element}
        digits = normalise(fields.get("bd_addr", ""), 12)
        if digits is None:
            print("skipping a device with an unusable bd_addr", file=sys.stderr)
            continue
        address = ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()
        key = normalise(fields.get("link_key", ""), 32)
        if key is None:
            print(f"skipping {address}: unusable link_key", file=sys.stderr)
            continue
        key_type = fields.get("key_type", "").strip()
        devices[address] = (key, int(key_type) if key_type.isdigit() else None)
    return devices


def build_payload(devices, reverse_key_bytes):
    peers = {}
    for address, (key, key_type) in devices.items():
        if reverse_key_bytes:
            key = bytes(reversed(bytes.fromhex(key))).hex()
        entry = {"link_key": {"value": key,
                              "authenticated": key_type in AUTHENTICATED_KEY_TYPES}}
        if key_type is not None:
            entry["link_key_type"] = key_type
        peers[address] = peers[address + "/P"] = entry
    return peers


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert a Kindle bt_devices.xml export into a Bumble JsonKeyStore file.",
        epilog="Prints Bluetooth addresses only. Never prints key material.",
    )
    parser.add_argument("input", type=Path, help="exported bt_devices.xml")
    parser.add_argument("-o", "--output", type=Path, required=True,
                        help="JSON keystore to create; must not exist")
    parser.add_argument("--namespace", default="__DEFAULT__", help="keystore namespace")
    parser.add_argument("--reverse-key-bytes", action="store_true",
                        help="reverse the 16 key bytes; the tested headset needs this")
    args = parser.parse_args(argv)

    try:
        devices = parse(args.input)
    except (OSError, ET.ParseError) as error:
        print(f"cannot read {args.input}: {error}", file=sys.stderr)
        return 1
    if not devices:
        print("no device with a usable bd_addr and link_key was found", file=sys.stderr)
        return 1

    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"refusing to overwrite existing file: {args.output}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"cannot create {args.output}: {error}", file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({args.namespace: build_payload(devices, args.reverse_key_bytes)},
                  handle, indent=4, sort_keys=True)
        handle.write("\n")

    print("\n".join(sorted(devices)))
    print(f"wrote {args.output}: {len(devices)} device(s), namespace {args.namespace}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
