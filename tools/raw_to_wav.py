#!/usr/bin/env python3
"""Wrap a headerless raw audio payload in a standard PCM WAV container.

The input is a flat byte stream with no header. The caller states the sample
rate and the byte order. The program writes a RIFF/WAVE file with 16-bit
samples and refuses to replace an existing output file. It does not prove that
the input is audio: encoded, truncated, or silent payloads convert without
error.
"""

import argparse
import os
import struct
import sys
import wave


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Wrap raw 16-bit PCM bytes in a WAV file.",
    )
    parser.add_argument("raw", help="input file with headerless samples")
    parser.add_argument(
        "rate",
        type=int,
        help="sample rate in Hz, for example 8000",
    )
    parser.add_argument("output", help="output .wav path; must not exist")
    parser.add_argument(
        "--channels",
        type=int,
        default=1,
        choices=(1, 2),
        help="interleaved channel count (default: 1)",
    )
    parser.add_argument(
        "--big-endian",
        action="store_true",
        help="read samples as big-endian instead of little-endian",
    )
    args = parser.parse_args(argv)

    if args.rate <= 0:
        parser.error("rate must be a positive integer")
    if args.raw == args.output:
        parser.error("output must differ from the input file")
    return args


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

    frame_bytes = 2 * args.channels
    remainder = len(payload) % frame_bytes
    if remainder:
        print(
            f"warning: dropping {remainder} trailing byte(s) that do not "
            f"form a complete {frame_bytes}-byte frame",
            file=sys.stderr,
        )
        payload = payload[: len(payload) - remainder]

    if not payload:
        print("error: input has no complete sample frames", file=sys.stderr)
        return 1

    order = ">" if args.big_endian else "<"
    sample_count = len(payload) // 2
    samples = struct.unpack(f"{order}{sample_count}h", payload)

    with wave.open(args.output, "wb") as wav:
        wav.setnchannels(args.channels)
        wav.setsampwidth(2)
        wav.setframerate(args.rate)
        wav.writeframes(payload)

    frames = sample_count // args.channels
    duration = frames / args.rate
    print(f"wrote {args.output}")
    print(f"  channels      {args.channels}")
    print(f"  sample rate   {args.rate} Hz")
    print(f"  sample width  16-bit")
    print(f"  byte order    {'big' if args.big_endian else 'little'}-endian")
    print(f"  frames        {frames}")
    print(f"  duration      {duration:.3f} s")
    print(f"  peak          {max(max(samples), -min(samples))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
