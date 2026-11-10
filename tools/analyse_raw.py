#!/usr/bin/env python3
"""Report whether a headerless payload looks like 16-bit mono PCM audio.

The program prints statistics only. It never prints sample values in file
order, so it does not expose recorded content. Use it to test the guess that a
raw HCI payload is signed 16-bit little-endian PCM at a stated sample rate.

The checks compare three readings of the same bytes: little-endian 16-bit,
big-endian 16-bit, and 8-bit. A real speech recording has a low fraction of
exact zeros, a high number of distinct sample values, and a sample-to-sample
correlation well below 1. Silence, constant data, and encoded frames each fail
at least one of these checks.

Only the Python standard library is used.
"""

import argparse
import collections
import math
import struct
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Print PCM plausibility statistics for a raw payload.",
    )
    parser.add_argument("raw", help="input file with headerless samples")
    parser.add_argument(
        "rate",
        type=int,
        help="sample rate in Hz that the payload is assumed to use",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=0.5,
        help="window length in seconds for the per-window table (default: 0.5)",
    )
    return parser.parse_args(argv)


def describe(samples, label):
    """Print one summary line block for a sequence of integer samples."""
    count = len(samples)
    mean = sum(samples) / count
    rms = math.sqrt(sum((v - mean) ** 2 for v in samples) / count)
    peak = max(max(samples), -min(samples))
    exact_zero = sum(1 for v in samples if v == 0) / count
    near_zero = sum(1 for v in samples if abs(v) <= 1) / count
    distinct = len(set(samples))

    print(f"  {label}")
    print(f"    samples            {count}")
    print(f"    mean               {mean:.1f}")
    print(f"    rms                {rms:.1f}")
    print(f"    peak               {peak}")
    print(f"    exact-zero         {exact_zero:.4f}")
    print(f"    near-zero (|v|<=1) {near_zero:.4f}")
    print(f"    distinct values    {distinct}")
    return {"rms": rms, "peak": peak, "near_zero": near_zero,
            "exact_zero": exact_zero, "distinct": distinct}


def lag_correlation(samples, lag):
    """Return the Pearson correlation between samples and samples shifted."""
    left = samples[:-lag]
    right = samples[lag:]
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    numerator = sum((a - mean_left) * (b - mean_right)
                    for a, b in zip(left, right))
    energy_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    energy_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if energy_left == 0 or energy_right == 0:
        return float("nan")
    return numerator / (energy_left * energy_right)


def longest_zero_run(samples):
    """Return the length of the longest run of exactly zero samples."""
    best = 0
    current = 0
    for value in samples:
        if value == 0:
            current += 1
            if current > best:
                best = current
        else:
            current = 0
    return best


def main(argv=None):
    args = parse_args(argv)

    with open(args.raw, "rb") as handle:
        payload = handle.read()

    if len(payload) < 2:
        print("error: input is too short to hold one sample", file=sys.stderr)
        return 1

    rate = args.rate
    even = payload[: len(payload) - (len(payload) % 2)]
    sample_count = len(even) // 2
    duration = sample_count / rate

    print("FILE")
    print(f"  bytes              {len(payload)}")
    print(f"  16-bit samples     {sample_count}")
    print(f"  duration at {rate} Hz  {duration:.3f} s")
    print(f"  byte rate          {len(payload) / duration:.0f} B/s")
    print()

    print("BYTE LEVEL")
    zero_bytes = payload.count(0)
    ff_bytes = payload.count(0xFF)
    print(f"  0x00 fraction      {zero_bytes / len(payload):.4f}")
    print(f"  0xFF fraction      {ff_bytes / len(payload):.4f}")
    print(f"  distinct values    {len(set(payload))}")
    best = 0
    current = 0
    for byte in payload:
        if byte == 0:
            current += 1
            if current > best:
                best = current
        else:
            current = 0
    print(f"  longest 0x00 run   {best} bytes")
    print()

    little = struct.unpack(f"<{sample_count}h", even)
    big = struct.unpack(f">{sample_count}h", even)

    print("READING AS SIGNED 16-BIT")
    little_stats = describe(little, "little-endian")
    big_stats = describe(big, "big-endian")
    print()

    unsigned = list(payload)
    print("READING AS 8-BIT (unsigned)")
    describe(unsigned, "8-bit unsigned")
    signed8 = [b - 256 if b > 127 else b for b in payload]
    describe(signed8, "8-bit signed")
    print()

    print("SAMPLE VALUES (little-endian, most common)")
    counts = collections.Counter(little)
    for value, count in counts.most_common(6):
        print(f"  value {value:7d}  count {count:6d}  "
              f"fraction {count / sample_count:.4f}")
    print()

    print("SPEECH-LIKENESS (little-endian)")
    print(f"  lag-1 correlation  {lag_correlation(little, 1):.4f}")
    print(f"  lag-2 correlation  {lag_correlation(little, 2):.4f}")
    run = longest_zero_run(little)
    print(f"  longest zero run   {run} samples = {run / rate:.3f} s")
    print()

    window_samples = max(1, int(round(args.window * rate)))
    print(f"PER-WINDOW TABLE ({args.window:g} s windows, little-endian)")
    print(f"  {'start':>7} {'rms':>9} {'peak':>7} {'near-zero':>10}")
    for start in range(0, sample_count, window_samples):
        chunk = little[start: start + window_samples]
        if not chunk:
            break
        mean = sum(chunk) / len(chunk)
        rms = math.sqrt(sum((v - mean) ** 2 for v in chunk) / len(chunk))
        peak = max(max(chunk), -min(chunk))
        near = sum(1 for v in chunk if abs(v) <= 1) / len(chunk)
        print(f"  {start / rate:7.2f} {rms:9.1f} {peak:7d} {near:10.4f}")
    print()

    print("VERDICT")
    looks_like_pcm = (
        little_stats["exact_zero"] < 0.5
        and little_stats["distinct"] > sample_count * 0.05
        and abs(lag_correlation(little, 1)) < 0.999
    )
    if looks_like_pcm:
        print("  the payload is consistent with 16-bit PCM audio")
    else:
        print("  the payload does NOT look like 16-bit PCM audio")
        if little_stats["exact_zero"] >= 0.5:
            print("  reason: most samples are exactly zero")
        if little_stats["distinct"] <= sample_count * 0.05:
            print("  reason: too few distinct sample values for audio")
        if abs(lag_correlation(little, 1)) >= 0.999:
            print("  reason: adjacent samples are almost identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
