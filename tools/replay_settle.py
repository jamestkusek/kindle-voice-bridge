#!/usr/bin/env python3
"""Replay recorded HFP captures through the settle detector. Offline only.

Each argument is a raw capture written by hfp_capture.py. The matching .json
sidecar supplies the packet boundaries. Nothing is written and nothing is
printed except summary numbers: no samples, no text, no audio.

This imports tools/hfp_capture.py, so it needs the same Bumble dependency.

    python tools/replay_settle.py .data/capture3.raw .data/capture.raw

Exit status is 0 when every capture was read, 1 when one could not be read.
"""

import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from hfp_capture import (
        SAMPLE_RATE,
        SETTLE_MEAN_LIMIT,
        SETTLE_RMS_MIN,
        SETTLE_WINDOW_SECONDS,
        SETTLE_ZCR_MIN,
        SettleDetector,
    )
except ModuleNotFoundError:
    print(
        "Cannot import tools/hfp_capture.py. Install the hci extra: "
        "python -m pip install -e '.[hci]'",
        file=sys.stderr,
    )
    sys.exit(2)


def packet_payloads(raw: bytes, packets: list) -> list:
    return [raw[entry["offset"]:entry["offset"] + entry["length"]] for entry in packets]


def describe(payload: bytes):
    """Return (mean, rms, zero crossings per second) for one payload."""
    count = len(payload) // 2
    if count == 0:
        return 0.0, 0.0, 0.0
    samples = struct.unpack(f"<{count}h", payload[:count * 2])
    mean = sum(samples) / count
    rms = math.sqrt(sum((value - mean) ** 2 for value in samples) / count)
    crossings = 0
    previous = samples[0]
    for value in samples:
        if (previous < 0 <= value) or (previous >= 0 > value):
            crossings += 1
        previous = value
    return mean, rms, crossings * SAMPLE_RATE / count


def replay(path: Path) -> bool:
    sidecar = Path(str(path) + ".json")
    if not path.is_file() or not sidecar.is_file():
        print(f"{path.name}: missing raw file or sidecar", file=sys.stderr)
        return False
    raw = path.read_bytes()
    packets = json.loads(sidecar.read_text(encoding="utf-8")).get("packets", [])
    payloads = packet_payloads(raw, packets)

    detector = SettleDetector()
    settled_at = None
    discarded = 0
    for index, payload in enumerate(payloads):
        if detector.feed(payload):
            settled_at = index + 1
            break
    if settled_at is not None:
        keep_from = int(round(detector.settle_seconds * SAMPLE_RATE))
        position = 0
        for payload in payloads:
            if position >= keep_from:
                break
            discarded += 1
            position += len(payload) // 2

    total_samples = sum(len(payload) // 2 for payload in payloads)
    print(f"{path.name}")
    print(f"  packets            {len(payloads)}")
    print(f"  duration           {total_samples / SAMPLE_RATE:.3f} s")
    if settled_at is None:
        print("  settle             never")
        print("  packets discarded  all (no audio would be written)")
        return True

    kept = payloads[discarded:]
    kept_samples = sum(len(payload) // 2 for payload in kept)
    stats = [describe(payload) for payload in kept]
    peak_dc = max((abs(entry[0]) for entry in stats), default=0.0)
    window_rms = math.sqrt(
        sum(entry[1] ** 2 * (len(payload) // 2) for entry, payload in zip(stats, kept))
        / max(1, kept_samples)
    )
    window_zcr = (
        sum(entry[2] * (len(payload) // 2) for entry, payload in zip(stats, kept))
        / max(1, kept_samples)
    )
    print(f"  settle             {detector.settle_seconds:.3f} s "
          f"(decided after packet {settled_at})")
    print(f"  packets discarded  {discarded}")
    print(f"  packets kept       {len(kept)} ({kept_samples / SAMPLE_RATE:.3f} s)")
    print(f"  criterion          {detector.criterion}")
    print(f"  kept max |mean|    {peak_dc:.1f}")
    print(f"  kept rms           {window_rms:.1f}")
    print(f"  kept zcr           {window_zcr:.0f} /s")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(
        f"thresholds: window {SETTLE_WINDOW_SECONDS:.2f} s, "
        f"max |packet mean| {SETTLE_MEAN_LIMIT:g}, "
        f"zcr {SETTLE_ZCR_MIN:g}/s, rms {SETTLE_RMS_MIN:g}",
        file=sys.stderr,
    )
    ok = True
    for name in argv:
        ok = replay(Path(name)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
