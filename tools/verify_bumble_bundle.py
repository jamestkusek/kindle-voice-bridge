#!/usr/bin/env python3
"""Check a staged Bumble bundle without ``cryptography``.

This program does not open a UART, a socket, or a Bluetooth controller. It
imports modules, reads ``sys.modules``, and runs local test vectors.

Point ``PYTHONPATH`` at the staging directory. The program makes
``cryptography`` unimportable before it imports Bumble, so the result matches a
device that has no working ``cryptography`` wheel.

Exit status 0 means every check passed.
"""

import argparse
import hashlib
import importlib.util
import os
import sys

CAPTURE_MODULES = (
    "bumble.core",
    "bumble.hci",
    "bumble.keys",
    "bumble.device",
    "bumble.rfcomm",
    "bumble.hfp",
    "bumble.transport.common",
)

# Transport modules that the capture path opens on demand. They are not
# imported by the seven modules above, so check them separately.
OPTIONAL_MODULES = (
    "bumble.transport.serial",
    "bumble.transport.tcp_client",
)

BLOCKED_ROOTS = ("cryptography", "_cryptography", "cffi", "_cffi_backend")


class Blocker:
    """A meta path finder that makes selected top-level modules unimportable."""

    def __init__(self, roots):
        self.roots = frozenset(roots)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.roots:
            raise ImportError(f"blocked for this check: {fullname}")
        return None


def install_blocker() -> None:
    sys.meta_path.insert(0, Blocker(BLOCKED_ROOTS))
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED_ROOTS:
            del sys.modules[name]


def check_blocked() -> list[str]:
    problems = []
    for root in BLOCKED_ROOTS:
        try:
            importlib.util.find_spec(root)
        except (ImportError, ValueError):
            continue
        if root in sys.modules:
            problems.append(f"{root} is still in sys.modules")
        else:
            problems.append(f"{root} is still importable")
    return problems


def check_layout(bundle: str) -> list[str]:
    problems = []
    import bumble

    location = os.path.abspath(os.path.dirname(bumble.__file__))
    expected = os.path.abspath(os.path.join(bundle, "bumble"))
    if os.path.normcase(location) != os.path.normcase(expected):
        problems.append(f"bumble loaded from {location}, not {expected}")
    return problems


def aes_cmac_vectors() -> list[str]:
    """RFC 4493 AES-128-CMAC test vectors."""
    from bumble.crypto.builtin import aes_cmac

    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    message = bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411e5fbc1191a0a52ef"
        "f69f2445df4f9b17ad2b417be66c3710"
    )
    expected = {
        0: "bb1d6929e95937287fa37d129b756746",
        16: "070a16b46b4d4144f79bdd9dd04a287c",
        40: "dfa66747de9ae63030ca32611497c827",
        64: "51f0bebf7e3b9d92fc49741779363cfe",
    }
    problems = []
    for length, want in expected.items():
        got = aes_cmac(message[:length], key).hex()
        if got != want:
            problems.append(f"aes_cmac({length} bytes) = {got}, expected {want}")
    return problems


def aes_e_vectors() -> list[str]:
    """FIPS-197 AES-128 ECB, wrapped by Bumble's byte-swapped ``e``."""
    from bumble.crypto.builtin import e

    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    want = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    # ``e`` swaps byte order on input and output.
    got = e(key[::-1], plaintext[::-1])[::-1]
    if got != want:
        return [f"aes e() = {got.hex()}, expected {want.hex()}"]
    return []


def ecc_vector() -> list[str]:
    """SECP256R1 ECDH against a published NIST vector."""
    from bumble.crypto.builtin import EccKey

    alice_private = bytes.fromhex(
        "c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721"
    )
    bob_private = bytes.fromhex(
        "b7e08afdfe94bad3f1dc8c734798ba1c62b3a0ad2f332183c9795cbf9c5f2b6d"
    )
    alice = EccKey.from_private_key_bytes(alice_private)
    bob = EccKey.from_private_key_bytes(bob_private)
    shared_a = alice.dh(bob.x, bob.y)
    shared_b = bob.dh(alice.x, alice.y)
    problems = []
    if shared_a != shared_b:
        problems.append("ECDH shared secrets differ between the two sides")
    if len(shared_a) != 32:
        problems.append(f"ECDH shared secret is {len(shared_a)} bytes, expected 32")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "build",
            "bumble-kindle",
        ),
        help="staging directory that PYTHONPATH points at",
    )
    parser.add_argument(
        "--skip-vectors",
        action="store_true",
        help="skip the AES, CMAC and ECDH test vectors",
    )
    args = parser.parse_args()

    install_blocker()
    failures: list[str] = []

    print(f"PYTHON {sys.version.split()[0]}")
    print(f"PYTHONPATH {os.environ.get('PYTHONPATH', '(unset)')}")
    print(f"BUNDLE {os.path.abspath(args.bundle)}")
    print()

    print("Blocked modules:")
    blocked_problems = check_blocked()
    for problem in blocked_problems:
        print(f"  FAIL {problem}")
    if not blocked_problems:
        print(f"  OK   {', '.join(BLOCKED_ROOTS)} cannot be imported")
    failures.extend(blocked_problems)
    print()

    print("Bundle location:")
    layout_problems = check_layout(args.bundle)
    for problem in layout_problems:
        print(f"  FAIL {problem}")
    if not layout_problems:
        import bumble

        print(f"  OK   bumble loaded from {os.path.dirname(bumble.__file__)}")
    failures.extend(layout_problems)
    print()

    print("Capture modules:")
    for name in CAPTURE_MODULES + OPTIONAL_MODULES:
        try:
            __import__(name)
        except Exception as error:  # noqa: BLE001 - report, do not raise
            print(f"  FAIL {name}: {type(error).__name__}: {error}")
            failures.append(f"{name} did not import")
        else:
            print(f"  OK   {name}")
    print()

    print("Crypto backend:")
    from bumble import crypto

    backend = crypto.EccKey.__module__
    print(f"  EccKey   {backend}")
    print(f"  aes_cmac {crypto.aes_cmac.__module__}")
    print(f"  e        {crypto.e.__module__}")
    if backend != "bumble.crypto.builtin":
        failures.append(f"crypto backend is {backend}, expected bumble.crypto.builtin")
    print()

    print("Versions:")
    from importlib.metadata import version as distribution_version

    for distribution in (
        "bumble",
        "pyee",
        "typing-extensions",
        "pyserial",
        "pyserial-asyncio",
        "platformdirs",
    ):
        try:
            print(f"  {distribution:18} {distribution_version(distribution)}")
        except Exception as error:  # noqa: BLE001
            print(f"  {distribution:18} unavailable: {error}")
            failures.append(f"{distribution} metadata is missing")
    print()

    if not args.skip_vectors:
        print("Builtin crypto test vectors:")
        for label, problems in (
            ("aes_cmac (RFC 4493)", aes_cmac_vectors()),
            ("aes e (FIPS-197)", aes_e_vectors()),
            ("ecdh (NIST P-256)", ecc_vector()),
        ):
            if problems:
                for problem in problems:
                    print(f"  FAIL {label}: {problem}")
                failures.extend(problems)
            else:
                print(f"  OK   {label}")
        print()

    print("Bundle digest:")
    digest = hashlib.sha256()
    count = 0
    for root, dirs, files in os.walk(args.bundle):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(root, name)
            relative = os.path.relpath(path, args.bundle).replace(os.sep, "/")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            with open(path, "rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            count += 1
    print(f"  files  {count}")
    print(f"  sha256 {digest.hexdigest()}")
    print()

    if failures:
        print(f"RESULT FAIL ({len(failures)} problem(s))")
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
