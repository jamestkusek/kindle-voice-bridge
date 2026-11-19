"""One-off repair of a trace left pending after its last stage succeeded.

A stuck trace means the pipeline finished but the final ``set_outcome`` write
lost a Windows file-sharing race. This script is not runtime logic: it fixes
the recorded files once, in place, and refuses anything it does not recognise.

    python tools/repair_pending_trace.py <recordings_dir> [--apply]
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

TRACE_SUFFIX = ".trace.json"


def is_stuck(trace: dict) -> bool:
    """Return True for a pending trace whose last stage is sidecars/ok."""

    if trace.get("outcome") != "pending":
        return False
    stages = trace.get("stages")
    if not isinstance(stages, list) or not stages:
        return False
    last = stages[-1]
    return (
        isinstance(last, dict)
        and last.get("name") == "sidecars"
        and last.get("outcome") == "ok"
    )


def write_atomic(path: pathlib.Path, body: str) -> None:
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=".repair-", suffix=".part"
    )
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__.strip())
        return 2
    directory = pathlib.Path(argv[0])
    apply = "--apply" in argv[1:]

    repaired = 0
    for path in sorted(directory.glob(f"*{TRACE_SUFFIX}")):
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"skip {path.name}: {type(error).__name__}")
            continue
        if not isinstance(trace, dict) or not is_stuck(trace):
            continue
        if not apply:
            print(f"would repair {path.name}")
            repaired += 1
            continue
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
        trace["outcome"] = "ok"
        write_atomic(path, json.dumps(trace, sort_keys=True) + "\n")
        print(f"repaired {path.name} -> outcome=ok (backup {backup.name})")
        repaired += 1
    print(f"{'repaired' if apply else 'would repair'}: {repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
