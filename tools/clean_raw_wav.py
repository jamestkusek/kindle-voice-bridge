#!/usr/bin/env python3
"""Remove DC offset from a headerless PCM payload and write a normalised WAV.

The input is a flat byte stream of signed 16-bit little-endian samples. The
program removes the DC offset, scales the result to a target peak level, and
refuses to replace an existing file. Removing an offset does not recover audio
that the capture never held. The optional high-pass filter tracks a drifting
baseline; set it below the lowest voice pitch you want to keep.
"""

import argparse
import math
import os
import struct
import sys
import wave


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove DC offset and normalise a raw PCM payload.",
    )
    parser.add_argument("raw", help="input file with headerless samples")
    parser.add_argument("rate", type=int, help="sample rate in Hz")
    parser.add_argument("output", help="output .wav path; must not exist")
    parser.add_argument(
        "--highpass",
        type=float,
        default=0.0,
        help="one-pole high-pass corner in Hz (default: 0, plain DC removal)",
    )
    parser.add_argument(
        "--start",
        type=float,
        default=None,
        help="start time in seconds (default: beginning)",
    )
    parser.add_argument(
        "--end",
        type=float,
        default=None,
        help="end time in seconds (default: end of file)",
    )
    parser.add_argument(
        "--peak-db",
        type=float,
        default=-1.0,
        help="target peak level in dBFS (default: -1.0)",
    )
    args = parser.parse_args(argv)

    if args.rate <= 0:
        parser.error("rate must be a positive integer")
    if args.raw == args.output:
        parser.error("output must differ from the input file")
    if args.peak_db >= 0:
        parser.error("peak-db must be below 0")
    if args.start is not None and args.start < 0:
        parser.error("start must not be negative")
    if args.end is not None and args.start is not None and args.end <= args.start:
        parser.error("end must be greater than start")
    return args


def highpass(samples, rate, corner):
    """Apply a one-pole high-pass filter that also removes DC."""
    dt = 1.0 / rate
    rc = 1.0 / (2.0 * math.pi * corner)
    alpha = rc / (rc + dt)
    out = []
    previous_in = samples[0]
    previous_out = 0.0
    for value in samples:
        current = alpha * (previous_out + value - previous_in)
        out.append(current)
        previous_in = value
        previous_out = current
    return out


def main(argv=None):
    args = parse_args(argv)

    if os.path.exists(args.output):
        print(
            f"refusing to overwrite existing file: {args.output}",
            file=sys.stderr,
        )
        return 1

    with open(args.raw, "rb") as handle:
        payload = handle.read()

    even = payload[: len(payload) - (len(payload) % 2)]
    count = len(even) // 2
    samples = list(struct.unpack(f"<{count}h", even))

    first = 0 if args.start is None else int(args.start * args.rate)
    last = count if args.end is None else int(args.end * args.rate)
    first = max(0, min(first, count))
    last = max(first, min(last, count))
    samples = samples[first:last]
    if not samples:
        print("error: selected range holds no samples", file=sys.stderr)
        return 1

    if args.highpass > 0:
        working = highpass(samples, args.rate, args.highpass)
    else:
        mean = sum(samples) / len(samples)
        working = [value - mean for value in samples]

    peak = max(max(working), -min(working))
    if peak <= 0:
        print("error: signal is constant; nothing to normalise", file=sys.stderr)
        return 1

    target = (10.0 ** (args.peak_db / 20.0)) * 32767.0
    scale = target / peak
    output = []
    for value in working:
        scaled = int(round(value * scale))
        output.append(max(-32768, min(32767, scaled)))

    frames = struct.pack(f"<{len(output)}h", *output)
    with wave.open(args.output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(args.rate)
        wav.writeframes(frames)

    print(f"wrote {args.output}")
    print(f"  range         {first / args.rate:.3f} - {last / args.rate:.3f} s")
    print(f"  frames        {len(output)}")
    print(f"  duration      {len(output) / args.rate:.3f} s")
    print(f"  high-pass     "
          f"{'off (mean removed)' if args.highpass <= 0 else f'{args.highpass:g} Hz'}")
    print(f"  gain          {20.0 * math.log10(scale):+.1f} dB")
    print(f"  peak          {max(max(output), -min(output))} ({args.peak_db:g} dBFS)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
