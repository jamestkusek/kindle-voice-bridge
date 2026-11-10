#!/usr/bin/env python3
"""Convert a Kindle zbluetooth pairing store into a Bumble JsonKeyStore file.

Input is a copy of /var/local/zbluetooth/bt_devices.xml. The tool never touches
the Kindle and never prints link key material; it prints Bluetooth addresses only.

Confirmed Bumble 0.0.236 schema (bumble/keys.py, class JsonKeyStore):

    {
        "<namespace>": {
            "<peer address>": {
                "link_key": {"value": "<32 lowercase hex chars>",
                             "authenticated": false},
                "link_key_type": 5
            }
        }
    }

Two details decide whether Bumble can actually find the key:

1. Namespace. JsonKeyStore.from_device() uses str(device.public_address) as the
   namespace, and Device.power_on() fills public_address from Read_BD_ADDR via
   Address.parse_address(), which tags it PUBLIC_DEVICE_ADDRESS. Bumble renders a
   public address as "AA:BB:CC:DD:EE:FF/P". A file namespaced "__DEFAULT__" is
   therefore NOT read when the keystore is attached to a device; pass the
   controller address with --namespace.

2. Peer key. Device.get_link_key() looks up str(peer_address) and the peer
   address arrives in a Link_Key_Request event, also parsed as public. So the
   peer key is "AA:BB:CC:DD:EE:FF/P" too. --peer-key-style picks the form.

The XML schema of bt_devices.xml could not be confirmed from any local sample, so
parsing is name-tolerant and the summary reports exactly which tags and
attributes matched.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXIT_OK = 0
EXIT_XML_ERROR = 3
EXIT_NO_KEYS = 4
EXIT_OUTPUT_EXISTS = 5
EXIT_IO_ERROR = 6

MAX_INPUT_BYTES = 8 * 1024 * 1024
OUTPUT_MODE = 0o600

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")
_SEPARATORS = re.compile(r"[\s:_\-.,]")

# Field names are compared after lowercasing and dropping non-alphanumerics, so
# "BD_ADDR", "bd-addr" and "bdAddr" all reduce to "bdaddr".
ADDRESS_NAMES = frozenset(
    {
        "address",
        "addr",
        "bdaddr",
        "bdaddress",
        "deviceaddress",
        "devaddr",
        "remoteaddress",
        "peeraddress",
        "pairedaddress",
        "btaddress",
        "bluetoothaddress",
        "mac",
        "macaddress",
    }
)

LOCAL_ADDRESS_NAMES = frozenset(
    {
        "localaddress",
        "localaddr",
        "localbdaddr",
        "localdeviceaddress",
        "localmac",
        "adapteraddress",
        "adapteraddr",
        "adapterbdaddr",
        "controlleraddress",
        "hostaddress",
        "selfaddress",
        "ownaddress",
    }
)

# Tier 0 names are unambiguous classic link keys. Tier 1 names are accepted but
# reported, because they can also carry an LE long-term key.
LINK_KEY_STRONG_NAMES = frozenset(
    {
        "linkkey",
        "linkkeyvalue",
        "linkkeyhex",
        "linkkeydata",
        "linkkeybytes",
        "btlinkkey",
        "lkey",
    }
)
LINK_KEY_WEAK_NAMES = frozenset(
    {"key", "keys", "keyvalue", "link", "ltk", "longtermkey"}
)

LINK_KEY_TYPE_NAMES = frozenset({"linkkeytype", "keytype", "linktype"})

# Attributes that hold a field name, and attributes that hold that field's value,
# for stores shaped like <Property name="LinkKey" value="00:11:..."/>.
NAME_ATTRS = ("name", "key", "id", "type", "field")
VALUE_ATTRS = ("value", "val", "data", "text")

AUTHENTICATED_KEY_TYPES = frozenset({0x05, 0x08})


# -----------------------------------------------------------------------------
# Errors and reporting
# -----------------------------------------------------------------------------
def fail(code: int, message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def note(message: str) -> None:
    print(message, file=sys.stderr)


# -----------------------------------------------------------------------------
# Value normalisation
# -----------------------------------------------------------------------------
def normalize_address(text: object) -> str | None:
    """Return "AA:BB:CC:DD:EE:FF" for a 48-bit address, else None."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value[:2].lower() == "0x":
        value = value[2:]
    value = _SEPARATORS.sub("", value)
    if len(value) != 12 or any(char not in _HEX_CHARS for char in value):
        return None
    value = value.upper()
    return ":".join(value[index : index + 2] for index in range(0, 12, 2))


def normalize_key(text: object) -> str | None:
    """Return 32 lowercase hex characters for a 128-bit key, else None."""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value[:2].lower() == "0x":
        value = value[2:]
    value = _SEPARATORS.sub("", value)
    if len(value) != 32 or any(char not in _HEX_CHARS for char in value):
        return None
    return value.lower()


def normalize_int(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    try:
        return int(value, 0)
    except ValueError:
        return None


def bumble_address_string(address: str) -> str:
    """Render an address the way Bumble renders a classic public address."""
    try:
        from bumble import hci  # Imported lazily: only needed for exact formatting.

        return str(hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS))
    except Exception:
        return f"{address}/P"


# -----------------------------------------------------------------------------
# XML field extraction
# -----------------------------------------------------------------------------
def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _normalize_name(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def _shallow_view(node: ET.Element) -> list[tuple[str, str, str]]:
    """Fields visible without descending more than one level.

    Used only to decide whether a child element is a self-contained device
    record, so the parent does not aggregate fields from two different devices.
    """
    fields = _node_fields(node)
    text = (node.text or "").strip()
    if text and len(node) == 0:
        fields.append((_normalize_name(_local_name(node.tag)), text, "text"))
    for child in node:
        child_text = (child.text or "").strip()
        if child_text and len(child) == 0:
            fields.append(
                (_normalize_name(_local_name(child.tag)), child_text, "text")
            )
        fields.extend(_node_fields(child))
    return fields


def _node_fields(node: ET.Element) -> list[tuple[str, str, str]]:
    """Fields carried by one element's own attributes."""
    fields: list[tuple[str, str, str]] = []
    attributes = {_normalize_name(key): value for key, value in node.attrib.items()}

    for name, value in attributes.items():
        fields.append((name, value, f"@{name}"))

    label = next((attributes[name] for name in NAME_ATTRS if name in attributes), None)
    if label is not None:
        for value_attr in VALUE_ATTRS:
            if value_attr in attributes:
                fields.append(
                    (
                        _normalize_name(label),
                        attributes[value_attr],
                        f'@{NAME_ATTRS[0]}="{label}"',
                    )
                )
    return fields


def element_fields(element: ET.Element) -> list[tuple[str, str, str]]:
    """Fields of an element: its attributes plus its direct children.

    Returns (normalised name, raw value, human readable origin) triples. The
    origin is only used in the summary, never with a value attached.
    """
    fields = _node_fields(element)

    for child in element:
        tag = _local_name(child.tag)
        tag_key = _normalize_name(tag)
        text = (child.text or "").strip()

        # A child that already carries both an address and a key is a device
        # record in its own right. Do not let the parent aggregate its fields,
        # which would pair one device's address with another device's key.
        shallow = _shallow_view(child)
        if _pick_address(shallow) and _pick_key(shallow):
            continue

        if text and len(child) == 0:
            fields.append((tag_key, text, f"<{tag}>"))

        for name, value, origin in _node_fields(child):
            if origin == "attribute":
                if name in VALUE_ATTRS:
                    fields.append((tag_key, value, f"<{tag}>@{name}"))
                elif name not in NAME_ATTRS:
                    fields.append((name, value, f"<{tag}>@{name}"))
            else:
                fields.append((name, value, origin))

    return fields


def _pick_address(fields: list[tuple[str, str, str]]) -> tuple[str, str] | None:
    for name, raw, origin in fields:
        if name in ADDRESS_NAMES:
            address = normalize_address(raw)
            if address is not None:
                return address, origin
    return None


def _pick_key(fields: list[tuple[str, str, str]]) -> tuple[int, str, str] | None:
    best: tuple[int, str, str] | None = None
    for name, raw, origin in fields:
        if name in LINK_KEY_STRONG_NAMES:
            tier = 0
        elif name in LINK_KEY_WEAK_NAMES:
            tier = 1
        else:
            continue
        key = normalize_key(raw)
        if key is None:
            continue
        if best is None or tier < best[0]:
            best = (tier, key, origin)
    return best


def _pick_link_key_type(fields: list[tuple[str, str, str]]) -> int | None:
    for name, raw, _origin in fields:
        if name in LINK_KEY_TYPE_NAMES:
            value = normalize_int(raw)
            if value is not None and 0x00 <= value <= 0x08:
                return value
    return None


def _iter_descendant_fields(element: ET.Element):
    """Yield field lists for descendants, skipping nested device elements."""
    for child in element:
        child_fields = element_fields(child)
        if _pick_address(child_fields) is not None:
            continue
        yield child_fields
        yield from _iter_descendant_fields(child)


# -----------------------------------------------------------------------------
# Extraction
# -----------------------------------------------------------------------------
@dataclasses.dataclass
class Stats:
    address_origins: collections.Counter = dataclasses.field(
        default_factory=collections.Counter
    )
    key_origins: collections.Counter = dataclasses.field(
        default_factory=collections.Counter
    )
    skipped_no_key: int = 0
    skipped_no_address: int = 0
    weak_key_fields: int = 0


@dataclasses.dataclass
class DeviceRecord:
    address: str
    key_hex: str
    address_origin: str
    key_origin: str
    key_tier: int
    link_key_type: int | None = None


def extract(
    root: ET.Element,
) -> tuple[list[DeviceRecord], str | None, Stats]:
    stats = Stats()
    records: dict[str, DeviceRecord] = {}
    record_elements: list[ET.Element] = []
    local_address: str | None = None

    for element in root.iter():
        fields = element_fields(element)

        if local_address is None:
            for name, raw, _origin in fields:
                if name in LOCAL_ADDRESS_NAMES:
                    candidate = normalize_address(raw)
                    if candidate is not None:
                        local_address = candidate
                        break

        address_hit = _pick_address(fields)
        if address_hit is None:
            continue

        address, address_origin = address_hit
        key_hit = _pick_key(fields)
        link_key_type = _pick_link_key_type(fields)
        if key_hit is None or link_key_type is None:
            for descendant_fields in _iter_descendant_fields(element):
                if key_hit is None:
                    key_hit = _pick_key(descendant_fields)
                if link_key_type is None:
                    link_key_type = _pick_link_key_type(descendant_fields)
                if key_hit is not None and link_key_type is not None:
                    break
        if key_hit is None:
            stats.skipped_no_key += 1
            continue

        tier, key_hex, key_origin = key_hit
        if tier > 0:
            stats.weak_key_fields += 1

        record = DeviceRecord(
            address=address,
            key_hex=key_hex,
            address_origin=address_origin,
            key_origin=key_origin,
            key_tier=tier,
            link_key_type=link_key_type,
        )
        record_elements.append(element)

        existing = records.get(address)
        if existing is None or record.key_tier < existing.key_tier:
            records[address] = record
            stats.address_origins[address_origin] += 1
            stats.key_origins[key_origin] += 1

    # Diagnostic only: elements that carry a key but no address, ignoring
    # elements that are part of a record already found above.
    covered: set[int] = set()
    for element in record_elements:
        covered.update(id(node) for node in element.iter())
    for element in root.iter():
        if id(element) in covered:
            continue
        fields = element_fields(element)
        if _pick_address(fields) is None and _pick_key(fields) is not None:
            stats.skipped_no_address += 1

    return sorted(records.values(), key=lambda item: item.address), local_address, stats


def describe_names(root: ET.Element, limit: int = 24) -> list[str]:
    """Field spellings seen in the document, for the diagnostic when nothing matched.

    Origins keep the original XML spelling (<BD_ADDR>, @link_key) so they can be
    compared with the real export. Values are never included.
    """
    counter: collections.Counter = collections.Counter()
    for element in root.iter():
        for name, _raw, origin in element_fields(element):
            if name:
                counter[origin] += 1
    return [f"{origin} ({count})" for origin, count in counter.most_common(limit)]


# -----------------------------------------------------------------------------
# Keystore payload
# -----------------------------------------------------------------------------
def peer_key_names(address: str, style: str) -> list[str]:
    qualified = bumble_address_string(address)
    if style == "bumble":
        return [qualified]
    if style == "plain":
        return [address]
    return [qualified] if qualified == address else [qualified, address]


def build_payload(
    namespace: str,
    devices: list[DeviceRecord],
    style: str,
    key_type_override: int | None,
    reverse_key_bytes: bool,
) -> dict[str, dict[str, dict]]:
    peer_map: dict[str, dict] = {}

    for device in devices:
        key_hex = device.key_hex
        if reverse_key_bytes:
            key_hex = bytes(reversed(bytes.fromhex(key_hex))).hex()

        key_type = key_type_override
        if key_type is None:
            key_type = device.link_key_type

        entry: dict[str, object] = {
            "link_key": {
                "value": key_hex,
                "authenticated": key_type in AUTHENTICATED_KEY_TYPES
                if key_type is not None
                else False,
            }
        }
        if key_type is not None:
            entry["link_key_type"] = key_type

        for name in peer_key_names(device.address, style):
            peer_map[name] = entry

    return {namespace: peer_map}


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------
def write_keystore(path: Path, payload: dict) -> int:
    if path.is_dir():
        return fail(EXIT_IO_ERROR, f"output path is a directory: {path}")
    if path.exists() or path.is_symlink():
        return fail(EXIT_OUTPUT_EXISTS, f"refusing to overwrite existing file: {path}")
    if not path.parent.exists():
        return fail(
            EXIT_IO_ERROR,
            f"output directory does not exist: {path.parent}",
        )

    data = json.dumps(payload, sort_keys=True, indent=4) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)

    try:
        descriptor = os.open(path, flags, OUTPUT_MODE)
    except FileExistsError:
        return fail(EXIT_OUTPUT_EXISTS, f"refusing to overwrite existing file: {path}")
    except OSError as error:
        return fail(
            EXIT_IO_ERROR,
            f"cannot create output file {path}: {error.strerror or error}",
        )

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        _discard(path)
        return fail(
            EXIT_IO_ERROR,
            f"cannot write output file {path}: {error.strerror or error}",
        )

    try:
        os.chmod(path, OUTPUT_MODE)
    except OSError:
        pass

    return EXIT_OK


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def verify_with_bumble(
    path: Path,
    namespace: str,
    devices: list[DeviceRecord],
    style: str,
    reverse_key_bytes: bool,
) -> str:
    """Round-trip the file through bumble.keys.JsonKeyStore. No key is printed."""
    try:
        from bumble.keys import JsonKeyStore
    except Exception:
        return "skipped (bumble is not importable by this interpreter)"

    async def check() -> list[str]:
        store = JsonKeyStore(namespace, str(path))
        missing: list[str] = []
        for device in devices:
            expected = device.key_hex
            if reverse_key_bytes:
                expected = bytes(reversed(bytes.fromhex(expected))).hex()

            found = False
            for name in peer_key_names(device.address, style):
                keys = await store.get(name)
                if keys is None or keys.link_key is None:
                    continue
                if keys.link_key.value.hex() == expected:
                    found = True
                    break
            if not found:
                missing.append(device.address)
        return missing

    try:
        missing = asyncio.run(check())
    except Exception as error:  # Defensive: a broken file must not look verified.
        return f"failed ({type(error).__name__})"

    if missing:
        return "MISMATCH for " + ", ".join(missing)
    return "JsonKeyStore round-trip OK"


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a copy of /var/local/zbluetooth/bt_devices.xml into a Bumble "
            "JsonKeyStore file. Prints Bluetooth addresses only."
        ),
        epilog=(
            "The keystore namespace must be the controller address, because "
            "JsonKeyStore.from_device() uses str(device.public_address) and Bumble "
            "renders a public address as AA:BB:CC:DD:EE:FF/P."
        ),
    )
    parser.add_argument("input", type=Path, help="path to the exported bt_devices.xml")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="path of the JSON keystore to create (required unless --dry-run)",
    )
    parser.add_argument(
        "--namespace",
        help=(
            "keystore namespace, normally the controller BD_ADDR. An address is "
            "normalised to Bumble's public form (suffix /P). Defaults to a local "
            "address found in the XML, else __DEFAULT__."
        ),
    )
    parser.add_argument(
        "--namespace-exact",
        action="store_true",
        help="use --namespace verbatim instead of normalising an address",
    )
    parser.add_argument(
        "--peer-key-style",
        choices=("bumble", "plain", "both"),
        default="both",
        help=(
            "form of the peer key: bumble (AA:BB:CC:DD:EE:FF/P), plain, or both "
            "(default; covers either lookup form)"
        ),
    )
    parser.add_argument(
        "--link-key-type",
        type=lambda value: int(value, 0),
        help="override the HCI link key type (0-8) recorded in the keystore",
    )
    parser.add_argument(
        "--reverse-key-bytes",
        action="store_true",
        help=(
            "reverse the 16 key bytes. Use only if authentication fails and the "
            "store is suspected to hold the key in the opposite byte order."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and print addresses without writing a keystore",
    )
    return parser


def load_xml(path: Path) -> ET.Element | int:
    if not path.exists():
        return fail(EXIT_IO_ERROR, f"input file does not exist: {path}")
    if path.is_dir():
        return fail(EXIT_IO_ERROR, f"input path is a directory: {path}")

    try:
        size = path.stat().st_size
    except OSError as error:
        return fail(EXIT_IO_ERROR, f"cannot stat input file {path}: {error}")

    if size == 0:
        return fail(EXIT_XML_ERROR, f"input file is empty: {path}")
    if size > MAX_INPUT_BYTES:
        return fail(
            EXIT_XML_ERROR,
            f"input file is larger than {MAX_INPUT_BYTES} bytes; refusing to parse",
        )

    try:
        data = path.read_bytes()
    except OSError as error:
        return fail(EXIT_IO_ERROR, f"cannot read input file {path}: {error}")

    if b"<!ENTITY" in data:
        return fail(
            EXIT_XML_ERROR,
            "input contains an XML entity declaration; refusing to parse",
        )

    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        return fail(EXIT_XML_ERROR, f"cannot parse XML in {path}: {error}")


def _print_addresses(devices: list[DeviceRecord]) -> None:
    """stdout carries addresses only, one per line, so it stays scriptable."""
    for device in devices:
        print(device.address)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.output is None and not args.dry_run:
        parser.error("--output is required unless --dry-run is used")

    if args.link_key_type is not None and not 0 <= args.link_key_type <= 0x08:
        parser.error("--link-key-type must be between 0 and 8")

    output_path: Path | None = None if args.dry_run else args.output
    if output_path is not None:
        if output_path.is_dir():
            return fail(EXIT_IO_ERROR, f"output path is a directory: {output_path}")
        if output_path.exists() or output_path.is_symlink():
            return fail(
                EXIT_OUTPUT_EXISTS, f"refusing to overwrite existing file: {output_path}"
            )

    root = load_xml(args.input)
    if isinstance(root, int):
        return root

    devices, local_address, stats = extract(root)

    if not devices:
        note(f"input: {args.input}")
        note("no device with both a Bluetooth address and a link key was found")
        note(f"  elements with an address but no link key: {stats.skipped_no_key}")
        note(f"  elements with a link key but no address: {stats.skipped_no_address}")
        note("  field spellings seen (with counts):")
        for description in describe_names(root):
            note(f"    {description}")
        return fail(
            EXIT_NO_KEYS,
            "expected fields are absent; nothing was written. The bt_devices.xml "
            "schema could not be confirmed, so the names above are the ones to "
            "compare against the real export.",
        )

    if args.namespace is not None:
        namespace = args.namespace
        namespace_source = "--namespace"
        if not args.namespace_exact:
            address = normalize_address(namespace)
            if address is not None:
                namespace = bumble_address_string(address)
    elif local_address is not None:
        namespace = bumble_address_string(local_address)
        namespace_source = "local address in the XML"
    else:
        namespace = "__DEFAULT__"
        namespace_source = "fallback, no local address found"

    payload = build_payload(
        namespace,
        devices,
        args.peer_key_style,
        args.link_key_type,
        args.reverse_key_bytes,
    )

    note(f"input: {args.input}")
    note(f"devices with an address and a link key: {len(devices)}")
    note(
        "address fields matched: "
        + (", ".join(f"{name} x{count}" for name, count in stats.address_origins.items()))
    )
    note(
        "link key fields matched: "
        + (", ".join(f"{name} x{count}" for name, count in stats.key_origins.items()))
    )
    if stats.weak_key_fields:
        note(
            f"warning: {stats.weak_key_fields} link key(s) came from a generic field "
            "name; confirm the keystore is a classic link key"
        )
    if stats.skipped_no_key:
        note(f"skipped: {stats.skipped_no_key} element(s) with an address but no key")
    if stats.skipped_no_address:
        note(f"skipped: {stats.skipped_no_address} element(s) with a key but no address")
    note(f"namespace: {namespace} (from {namespace_source})")
    note(
        "peer keys written: "
        + ", ".join(
            name
            for device in devices
            for name in peer_key_names(device.address, args.peer_key_style)
        )
    )
    if args.reverse_key_bytes:
        note("warning: key bytes were reversed as requested")
    if namespace == "__DEFAULT__":
        note(
            "warning: Bumble looks up keys under str(device.public_address), so a "
            "__DEFAULT__ namespace is ignored once the keystore is attached to a "
            "device. Pass --namespace <controller bd_addr>."
        )

    if args.dry_run:
        _print_addresses(devices)
        note("dry run: no keystore written")
        return EXIT_OK

    if output_path is None:
        return fail(EXIT_IO_ERROR, "no output path")

    result = write_keystore(output_path, payload)
    if result != EXIT_OK:
        return result

    note(f"output: {output_path} (mode {OUTPUT_MODE:04o})")
    if os.name == "nt":
        note("note: POSIX permission bits are advisory on Windows")
    note(
        "verification: "
        + verify_with_bumble(
            output_path,
            namespace,
            devices,
            args.peer_key_style,
            args.reverse_key_bytes,
        )
    )
    _print_addresses(devices)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
