#!/usr/bin/env python3
"""Transcribe recordings that the receiver has already stored.

This is the backfill tool. The receiver transcribes new uploads itself when
``OPENROUTER_API_KEY`` is set (see ``create_app_from_env``), so the worker is for
a backlog, for a storage directory the receiver does not own, or for a run with
a different model. Do not run it in a loop against a receiver that also has the
hook enabled: both would transcribe the same file.

It writes two sidecars next to each WAV through
:func:`voice_bridge_server.transcripts.transcribe_recording`:

    <id>.txt                the transcript, empty when there is no speech
    <id>.transcript.json    status, model, usage, attempts and timestamps

A file with a successful sidecar is never sent again. A failed file is retried
on the next poll up to ``--max-attempts`` and then left alone. The worker never
modifies, moves or deletes the WAV, and it never writes the API key anywhere.

The key is read from ``OPENROUTER_API_KEY`` unless ``--api-key-env`` names
another variable. Run with ``--once`` to drain the backlog and exit.
"""

import argparse
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server"))

from voice_bridge_server.storage import RECORDING_ID_PATTERN  # noqa: E402
from voice_bridge_server.transcribe import DEFAULT_MODEL  # noqa: E402
from voice_bridge_server.transcripts import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    is_pending,
    transcribe_recording,
)

DEFAULT_INTERVAL_SECONDS = 5.0


def pending_recordings(
    storage_dir: pathlib.Path, max_attempts: int
) -> list[pathlib.Path]:
    """Return the stored recordings that still need work, oldest first."""

    found = []
    try:
        entries = list(storage_dir.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file() or path.suffix.lower() != ".wav":
            continue
        if RECORDING_ID_PATTERN.fullmatch(path.stem) is None:
            continue
        if is_pending(path, max_attempts):
            found.append(path)
    found.sort(key=lambda item: item.stem)
    return found


def run_once(
    storage_dir: pathlib.Path,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    transport=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    report=print,
) -> int:
    """Transcribe every pending recording once. Returns the number handled."""

    handled = 0
    for wav_path in pending_recordings(storage_dir, max_attempts):
        data = transcribe_recording(
            wav_path, api_key, model=model, transport=transport, timeout=timeout
        )
        handled += 1
        if data.get("status") == "ok":
            text = data.get("text") or ""
            preview = text if len(text) <= 70 else text[:67] + "..."
            report(f"{wav_path.stem}: {preview or '(no speech)'}")
        else:
            report(f"{wav_path.stem}: {data.get('error', 'failed')}")
    return handled


def run_forever(
    storage_dir: pathlib.Path,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    transport=None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    report=print,
) -> None:
    """Poll the storage directory until interrupted."""

    report(f"Watching {storage_dir} for new recordings with {model}.")
    while True:
        try:
            run_once(
                storage_dir,
                api_key,
                model=model,
                max_attempts=max_attempts,
                transport=transport,
                timeout=timeout,
                report=report,
            )
        except KeyboardInterrupt:
            raise
        except Exception as error:  # keep the watcher alive
            report(f"The worker hit an unexpected error: {type(error).__name__}.")
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Transcribe stored recordings through OpenRouter.",
    )
    parser.add_argument(
        "--storage-dir",
        default=os.environ.get("KVB_STORAGE_DIR"),
        metavar="DIR",
        help="receiver storage directory. Defaults to KVB_STORAGE_DIR.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter model. The default is {DEFAULT_MODEL}.",
    )
    parser.add_argument(
        "--api-key-env",
        default="OPENROUTER_API_KEY",
        metavar="NAME",
        help="environment variable that holds the API key. "
        "The default is OPENROUTER_API_KEY.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        metavar="SECONDS",
        help=f"poll interval. The default is {DEFAULT_INTERVAL_SECONDS}.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        metavar="N",
        help=f"attempts per recording before it is left alone. "
        f"The default is {DEFAULT_MAX_ATTEMPTS}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"per-request timeout. The default is {DEFAULT_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="transcribe the current backlog and exit.",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.storage_dir:
        print(
            "error Set --storage-dir or KVB_STORAGE_DIR to the receiver storage directory.",
            file=sys.stderr,
        )
        return 2
    storage_dir = pathlib.Path(args.storage_dir).expanduser()
    if not storage_dir.is_dir():
        print(f"error The storage directory does not exist: {storage_dir}", file=sys.stderr)
        return 2
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        print(
            f"error Set {args.api_key_env} to an OpenRouter API key.",
            file=sys.stderr,
        )
        return 2
    if args.max_attempts < 1:
        print("error --max-attempts must be at least 1.", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("error --interval must be greater than zero.", file=sys.stderr)
        return 2

    try:
        if args.once:
            handled = run_once(
                storage_dir,
                api_key,
                model=args.model,
                max_attempts=args.max_attempts,
                timeout=args.timeout,
            )
            print(f"Handled {handled} recording(s).")
            return 0
        run_forever(
            storage_dir,
            api_key,
            model=args.model,
            max_attempts=args.max_attempts,
            interval=args.interval,
            timeout=args.timeout,
        )
    except KeyboardInterrupt:
        print("Stopped.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
