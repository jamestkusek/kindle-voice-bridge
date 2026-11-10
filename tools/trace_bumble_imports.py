#!/usr/bin/env python3
"""Report which modules the Bumble capture path imports, and what breaks.

This program does not open a UART, a socket, or a Bluetooth controller. It only
imports modules and reads ``sys.modules``.

Run it with ``--block cryptography`` to simulate the Kindle, where no
``cryptography`` wheel matches the installed glibc.
"""

import argparse
import sys
import types

CAPTURE_MODULES = (
    "bumble.core",
    "bumble.hci",
    "bumble.keys",
    "bumble.device",
    "bumble.rfcomm",
    "bumble.hfp",
    "bumble.transport.common",
)

BLOCKABLE = (
    "cryptography",
    "_cryptography",
    "cffi",
    "_cffi_backend",
    "libusb1",
    "usb",
    "usb1",
    "websockets",
    "prompt_toolkit",
    "click",
)


class Blocker:
    """A meta path finder that makes selected top-level modules unimportable."""

    def __init__(self, names):
        self.names = frozenset(names)

    def find_module(self, fullname, path=None):  # pragma: no cover - legacy API
        return None

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in self.names:
            raise ImportError(f"blocked by trace_bumble_imports: {fullname}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--block",
        action="append",
        default=[],
        choices=BLOCKABLE,
        help="make this top-level module unimportable (repeatable)",
    )
    parser.add_argument(
        "--modules",
        default=",".join(CAPTURE_MODULES),
        help="comma-separated module list to import",
    )
    parser.add_argument(
        "--list-third-party",
        action="store_true",
        help="print the third-party modules that ended up in sys.modules",
    )
    args = parser.parse_args()

    if args.block:
        sys.meta_path.insert(0, Blocker(args.block))

    failures = []
    for name in [item.strip() for item in args.modules.split(",") if item.strip()]:
        try:
            __import__(name)
        except Exception as error:  # noqa: BLE001 - report, do not raise
            failures.append((name, type(error).__name__, str(error)))
            print(f"IMPORT FAIL {name}: {type(error).__name__}: {error}")
        else:
            print(f"IMPORT OK   {name}")

    print()
    try:
        from bumble import crypto
    except Exception as error:  # noqa: BLE001
        print(f"CRYPTO BACKEND unavailable: {type(error).__name__}: {error}")
    else:
        print(f"CRYPTO BACKEND EccKey={crypto.EccKey.__module__}")
        print(f"CRYPTO BACKEND aes_cmac={crypto.aes_cmac.__module__}")
        print(f"CRYPTO BACKEND e={crypto.e.__module__}")

    try:
        from bumble._version import version

        print(f"BUMBLE VERSION {version}")
    except Exception as error:  # noqa: BLE001
        print(f"BUMBLE VERSION unavailable: {error}")

    print(f"PYTHON {sys.version.split()[0]}")

    if args.list_third_party:
        print()
        print("THIRD-PARTY MODULES IN sys.modules")
        stdlib = set(sys.stdlib_module_names)
        roots = sorted(
            {
                name.split(".")[0]
                for name in sys.modules
                if not name.startswith("_")
                and name.split(".")[0] not in stdlib
                and name.split(".")[0] not in {"bumble"}
            }
        )
        for root in roots:
            print(f"  {root}")

    if failures:
        print()
        print(f"{len(failures)} module(s) failed to import.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
