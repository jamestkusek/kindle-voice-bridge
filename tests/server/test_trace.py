"""Offline tests for the per-ask trace store and the stage log.

No test touches the network or the real storage directory. Each test gets its
own temporary directory, and every trace assumption is made against the file on
disk rather than against the object that wrote it.
"""

import json
import logging
import os
import pathlib
import time

import pytest

from voice_bridge_server import trace as trace_module
from voice_bridge_server.trace import (
    LOG_BUFFER,
    LOG_BUFFER_SIZE,
    MAX_STRING_CHARS,
    MAX_TRACE_BYTES,
    TraceStore,
    configure_logging,
    log_stage,
)

RECORDING_ID = "rec_20260101T000000Z_" + "a" * 32
OTHER_ID = "rec_20260101T000001Z_" + "b" * 32
THIRD_ID = "rec_20260101T000002Z_" + "c" * 32


def read_body(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def write_trace(directory: pathlib.Path, recording_id: str, **overrides) -> pathlib.Path:
    """Write a trace file directly, so ordering and shape can be controlled."""

    trace = {
        "v": 1,
        "id": recording_id,
        "origin": "kindle",
        "created_at": "2026-01-01T00:00:00+00:00",
        "outcome": "pending",
        "context": {
            "header_bytes": 10,
            "parsed": {"book": {"title": "A Book"}, "pos": {"page": 7}},
            "rejected_reason": None,
        },
        "book": {"key": "bk_0123456789abcdef", "match": "md5-alias"},
        "stages": [
            {"name": "receive", "started_at": "", "dur_ms": 3, "outcome": "ok",
             "detail": {}},
        ],
        "resolve": {"rung": "none", "reason": "", "passage": ""},
        "llm": {"model": "m", "prompt": {"system": "", "user": ""}, "usage": {},
                "cost_usd": 0.5, "cost_source": "response", "response_text": ""},
        "answer": {"chars_declared": 0, "chars_sent": 0},
    }
    trace.update(overrides)
    path = directory / f"{recording_id}.trace.json"
    path.write_text(json.dumps(trace), encoding="utf-8")
    return path


@pytest.fixture()
def storage(tmp_path):
    return tmp_path / "storage"


@pytest.fixture()
def store(storage):
    storage.mkdir()
    return TraceStore(storage)


@pytest.fixture()
def saved_logging():
    """Restore the level of the loggers these tests change."""

    root = logging.getLogger()
    voice = logging.getLogger("voice_bridge_server")
    root_level, voice_level = root.level, voice.level
    LOG_BUFFER.clear()
    yield
    root.setLevel(root_level)
    voice.setLevel(voice_level)
    LOG_BUFFER.clear()


# Header and round trip -----------------------------------------------------


def test_path_is_one_file_per_ask(store, storage):
    assert store.path(RECORDING_ID) == storage / f"{RECORDING_ID}.trace.json"


def test_start_writes_the_plan_header(store):
    store.start(RECORDING_ID, origin="kindle")

    data = store.read(RECORDING_ID)

    assert data["v"] == 1
    assert data["id"] == RECORDING_ID
    assert data["origin"] == "kindle"
    assert data["outcome"] == "pending"
    assert data["stages"] == []
    assert data["context"] == {
        "header_bytes": 0,
        "parsed": {},
        "rejected_reason": None,
    }
    assert data["context"]["rejected_reason"] is None
    assert data["resolve"]["rung"] == "none"
    assert data["llm"]["cost_usd"] is None
    # created_at is UTC ISO 8601 and carries a zone.
    created = data["created_at"]
    assert created.endswith("+00:00")
    parsed = trace_module.datetime.fromisoformat(created)
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_start_records_the_context_section(store):
    store.start(
        RECORDING_ID,
        origin="kindle",
        context={"header_bytes": 42, "parsed": {"highlight": "hello"}},
    )

    context = store.read(RECORDING_ID)["context"]

    assert context["header_bytes"] == 42
    assert context["parsed"] == {"highlight": "hello"}
    assert context["rejected_reason"] is None


def test_start_replaces_an_existing_trace(store):
    store.start(RECORDING_ID, origin="kindle")
    store.stage(RECORDING_ID, "receive")

    store.start(RECORDING_ID, origin="dev")

    data = store.read(RECORDING_ID)
    assert data["origin"] == "dev"
    assert data["stages"] == []


def test_round_trip_sections_stages_and_outcome(store):
    store.start(RECORDING_ID, origin="kindle")
    store.set(RECORDING_ID, "book", {"key": "bk_1", "match": "title+size"})
    store.set(RECORDING_ID, "resolve", {"rung": "highlight", "passage": "text"})
    store.stage(
        RECORDING_ID,
        "receive",
        duration_ms=3,
        detail={"bytes": 1200},
    )
    store.stage(RECORDING_ID, "context", outcome="failed", detail={"why": "bad"})
    store.set_outcome(RECORDING_ID, "ok")

    data = store.read(RECORDING_ID)

    assert data["book"] == {"key": "bk_1", "match": "title+size"}
    assert data["resolve"]["rung"] == "highlight"
    assert data["resolve"]["passage"] == "text"
    assert data["outcome"] == "ok"
    assert [stage["name"] for stage in data["stages"]] == ["receive", "context"]

    first = data["stages"][0]
    assert first["dur_ms"] == 3.0
    assert first["detail"] == {"bytes": 1200}
    assert first["outcome"] == "ok"
    assert first["started_at"]
    assert data["stages"][1]["outcome"] == "failed"
    assert data["resolve"]["reason"] == ""


def test_set_merges_a_mapping_into_the_section(store):
    store.start(RECORDING_ID, origin="kindle")

    store.set(RECORDING_ID, "llm", {"model": "m1"})
    store.set(RECORDING_ID, "llm", {"prompt": {"system": "s"}})

    llm = store.read(RECORDING_ID)["llm"]
    assert llm["model"] == "m1"
    assert llm["prompt"] == {"system": "s", "user": ""}
    assert llm["usage"] == {}


def test_timed_measures_the_block(store):
    store.start(RECORDING_ID, origin="kindle")

    with store.timed(RECORDING_ID, "answer"):
        time.sleep(0.02)

    stage = store.read(RECORDING_ID)["stages"][-1]
    assert stage["name"] == "answer"
    assert stage["outcome"] == "ok"
    assert stage["dur_ms"] >= 5


def test_timed_records_a_failure_and_reraises(store):
    store.start(RECORDING_ID, origin="kindle")

    with pytest.raises(RuntimeError):
        with store.timed(RECORDING_ID, "answer"):
            raise RuntimeError("boom")

    stage = store.read(RECORDING_ID)["stages"][-1]
    assert stage["name"] == "answer"
    assert stage["outcome"] == "failed"
    assert stage["detail"] == {"error": "RuntimeError"}


def test_stage_before_start_raises(store):
    with pytest.raises(FileNotFoundError):
        store.stage(RECORDING_ID, "receive")


def test_unknown_section_and_outcome_are_rejected(store):
    store.start(RECORDING_ID, origin="kindle")

    with pytest.raises(ValueError):
        store.set(RECORDING_ID, "nonsense", {})
    with pytest.raises(ValueError):
        store.set_outcome(RECORDING_ID, "maybe")
    with pytest.raises(ValueError):
        store.stage(RECORDING_ID, "")


# Caps ----------------------------------------------------------------------


def test_long_detail_value_is_replaced_by_a_marker(store):
    store.start(RECORDING_ID, origin="kindle")
    long_text = "x" * 5000

    store.stage(RECORDING_ID, "receive", detail={"note": long_text})

    body = read_body(store.path(RECORDING_ID))
    data = store.read(RECORDING_ID)

    assert long_text not in body
    assert "xxxx" not in body
    assert data["stages"][0]["detail"]["note"] == "[truncated 5000 chars]"
    assert len(data["stages"][0]["detail"]["note"]) < 64


def test_a_value_at_the_cap_is_kept(store):
    store.start(RECORDING_ID, origin="kindle")
    exact = "y" * MAX_STRING_CHARS

    store.stage(RECORDING_ID, "receive", detail={"note": exact})

    assert store.read(RECORDING_ID)["stages"][0]["detail"]["note"] == exact


def test_whole_trace_stays_under_the_byte_cap(store):
    store.start(RECORDING_ID, origin="kindle")
    block = "z" * MAX_TRACE_BYTES

    for index in range(20):
        store.stage(RECORDING_ID, f"stage{index}", detail={"note": block})

    path = store.path(RECORDING_ID)
    assert path.stat().st_size <= MAX_TRACE_BYTES
    data = store.read(RECORDING_ID)
    assert isinstance(data, dict)
    # The identity of the ask survives the trimming.
    assert data["id"] == RECORDING_ID
    # Every surviving string is inside the per-field cap.
    for stage in data["stages"]:
        note = stage["detail"]["note"]
        assert len(note) <= MAX_STRING_CHARS


def test_trace_survives_a_flood_of_many_small_values(store):
    store.start(RECORDING_ID, origin="kindle")

    for index in range(80):
        store.stage(RECORDING_ID, "receive", detail={"n": f"{index:03d}" * 40})

    data = store.read(RECORDING_ID)
    assert data is not None
    assert store.path(RECORDING_ID).stat().st_size <= MAX_TRACE_BYTES


# Retention flags -----------------------------------------------------------


def test_response_text_is_dropped_by_default(storage):
    storage.mkdir()
    store = TraceStore(storage)
    store.start(RECORDING_ID, origin="kindle")

    store.set(RECORDING_ID, "llm", {"response_text": "the model said this"})

    assert store.read(RECORDING_ID)["llm"]["response_text"] == ""


def test_raw_keeps_the_response_text(storage):
    storage.mkdir()
    store = TraceStore(storage, raw=True)
    store.start(RECORDING_ID, origin="kindle")

    store.set(RECORDING_ID, "llm", {"response_text": "the model said this"})

    assert store.read(RECORDING_ID)["llm"]["response_text"] == "the model said this"


def test_text_true_keeps_prompt_passage_and_context_text(store):
    store.start(
        RECORDING_ID,
        origin="kindle",
        context={"parsed": {"highlight": "a highlight", "book": {"title": "T"}}},
    )
    store.set(
        RECORDING_ID,
        "llm",
        {"prompt": {"system": "sys", "user": "usr"}, "response_text": "r"},
    )
    store.set(RECORDING_ID, "resolve", {"passage": "some passage"})

    data = store.read(RECORDING_ID)

    assert data["llm"]["prompt"] == {"system": "sys", "user": "usr"}
    assert data["resolve"]["passage"] == "some passage"
    assert data["context"]["parsed"]["highlight"] == "a highlight"
    assert data["context"]["parsed"]["book"]["title"] == "T"


def test_text_false_drops_prompt_passage_and_context_text(storage):
    storage.mkdir()
    store = TraceStore(storage, raw=True, text=False)
    store.start(
        RECORDING_ID,
        origin="kindle",
        context={
            "parsed": {
                "highlight": "a highlight",
                "book": {"title": "T", "authors": ["A"], "size_bytes": 12},
                "pos": {"page": 7, "chapter": "chapter one"},
            }
        },
    )
    store.set(
        RECORDING_ID,
        "llm",
        {"prompt": {"system": "sys", "user": "usr"}, "response_text": "kept"},
    )
    store.set(RECORDING_ID, "resolve", {"passage": "some passage", "rung": "highlight"})

    data = store.read(RECORDING_ID)

    assert data["llm"]["prompt"] == {"system": "", "user": ""}
    assert data["llm"]["response_text"] == "kept"
    assert data["resolve"]["passage"] == ""
    assert data["resolve"]["rung"] == "highlight"
    assert data["context"]["parsed"]["highlight"] == ""
    assert data["context"]["parsed"]["book"]["title"] == ""
    assert data["context"]["parsed"]["book"]["authors"] == []
    assert data["context"]["parsed"]["book"]["size_bytes"] == 12
    assert data["context"]["parsed"]["pos"]["chapter"] == ""
    assert data["context"]["parsed"]["pos"]["page"] == 7


# Listing -------------------------------------------------------------------


def test_list_recent_is_newest_first_with_summaries(storage):
    storage.mkdir()
    write_trace(storage, RECORDING_ID, created_at="2026-01-01T00:00:00+00:00")
    write_trace(
        storage,
        OTHER_ID,
        created_at="2026-01-01T00:00:05+00:00",
        outcome="ok",
        stages=[
            {"name": "receive", "started_at": "", "dur_ms": 2, "outcome": "ok",
             "detail": {}},
            {"name": "answer", "started_at": "", "dur_ms": 4, "outcome": "ok",
             "detail": {}},
        ],
        llm={"cost_usd": 0.25},
    )
    write_trace(storage, THIRD_ID, created_at="2026-01-01T00:00:09+00:00")

    rows = TraceStore(storage).list_recent()

    assert [row["id"] for row in rows] == [THIRD_ID, OTHER_ID, RECORDING_ID]
    newest = rows[1]
    assert newest["origin"] == "kindle"
    assert newest["outcome"] == "ok"
    assert newest["created_at"] == "2026-01-01T00:00:05+00:00"
    assert newest["stage"] == "answer"
    assert newest["stage_outcome"] == "ok"
    assert newest["total_ms"] == 6.0
    assert newest["book_title"] == "A Book"
    assert newest["book_page"] == 7
    assert newest["cost_usd"] == 0.25


def test_list_recent_honours_the_limit(storage):
    storage.mkdir()
    write_trace(storage, RECORDING_ID, created_at="2026-01-01T00:00:00+00:00")
    write_trace(storage, OTHER_ID, created_at="2026-01-01T00:00:05+00:00")

    store = TraceStore(storage)

    assert len(store.list_recent(limit=1)) == 1
    assert store.list_recent(limit=0) == []
    assert store.list_recent(limit=-3) == []


def test_list_recent_skips_a_corrupt_file(storage):
    storage.mkdir()
    write_trace(storage, RECORDING_ID)
    (storage / f"{OTHER_ID}.trace.json").write_text("not json", encoding="utf-8")

    rows = TraceStore(storage).list_recent()

    assert [row["id"] for row in rows] == [RECORDING_ID]


def test_list_recent_is_empty_without_a_directory(tmp_path):
    assert TraceStore(tmp_path / "missing").list_recent() == []


# Atomic write --------------------------------------------------------------


def test_a_failed_write_leaves_the_previous_trace(store, monkeypatch):
    store.start(RECORDING_ID, origin="kindle")

    def exploding_fsync(descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(trace_module.os, "fsync", exploding_fsync)

    with pytest.raises(OSError):
        store.stage(RECORDING_ID, "receive")

    data = store.read(RECORDING_ID)
    assert data["stages"] == []
    assert store.path(RECORDING_ID).is_file()


def test_no_temporary_file_survives(store):
    store.start(RECORDING_ID, origin="kindle")

    for index in range(3):
        store.stage(RECORDING_ID, f"stage{index}")

    left = [path.name for path in store.storage_dir.iterdir() if path.name !=
            f"{RECORDING_ID}.trace.json"]
    assert left == []


def test_the_trace_is_never_written_in_place(store, monkeypatch):
    """A rename is used, so a reader cannot see a partial document."""

    store.start(RECORDING_ID, origin="kindle")
    calls = []
    original = trace_module.os.replace

    def recording_replace(source, destination):
        calls.append((pathlib.Path(source), pathlib.Path(destination)))
        return original(source, destination)

    monkeypatch.setattr(trace_module.os, "replace", recording_replace)

    store.stage(RECORDING_ID, "receive")

    assert len(calls) == 1
    source, destination = calls[0]
    assert destination == store.path(RECORDING_ID)
    assert source.parent == destination.parent
    assert source != destination


def test_a_sharing_violation_on_replace_is_retried(store, monkeypatch):
    """A reader holds the old file open for a moment. Retry, do not lose."""

    store.start(RECORDING_ID, origin="kindle")
    calls = []
    original = trace_module.os.replace

    def flaky_replace(source, destination):
        calls.append(pathlib.Path(destination))
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation")
        return original(source, destination)

    monkeypatch.setattr(trace_module.os, "replace", flaky_replace)

    store.stage(RECORDING_ID, "receive")

    assert len(calls) == 3
    data = store.read(RECORDING_ID)
    assert [stage["name"] for stage in data["stages"]] == ["receive"]
    assert store.path(RECORDING_ID).is_file()


def test_a_persistent_sharing_violation_raises_and_leaves_no_temporary(
    store, monkeypatch
):
    store.start(RECORDING_ID, origin="kindle")
    calls = []

    def busy_replace(source, destination):
        calls.append(pathlib.Path(source))
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(trace_module.os, "replace", busy_replace)

    with pytest.raises(PermissionError):
        store.stage(RECORDING_ID, "receive")

    assert len(calls) == trace_module._RETRY_ATTEMPTS
    left = [
        path.name
        for path in store.storage_dir.iterdir()
        if path.name != f"{RECORDING_ID}.trace.json"
    ]
    assert left == []
    # The previous trace is still readable and unchanged.
    assert store.read(RECORDING_ID)["stages"] == []


def test_another_os_error_is_not_retried(store, monkeypatch):
    store.start(RECORDING_ID, origin="kindle")
    calls = []

    def failing_replace(source, destination):
        calls.append(pathlib.Path(source))
        raise OSError(2, "no such file")

    monkeypatch.setattr(trace_module.os, "replace", failing_replace)

    with pytest.raises(OSError):
        store.stage(RECORDING_ID, "receive")

    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_the_trace_is_private_to_the_owner(store):
    store.start(RECORDING_ID, origin="kindle")

    assert store.path(RECORDING_ID).stat().st_mode & 0o777 == 0o600


# Reading and validation ----------------------------------------------------


def test_read_returns_none_for_a_missing_trace(store):
    assert store.read(RECORDING_ID) is None


def test_read_returns_none_for_a_corrupt_trace(store):
    store.path(RECORDING_ID).write_text("{", encoding="utf-8")

    assert store.read(RECORDING_ID) is None


def test_read_retries_a_transient_permission_error(store, monkeypatch):
    store.start(RECORDING_ID, origin="kindle")
    real_open = open
    calls = []

    def flaky_open(*args, **kwargs):
        calls.append(args[0])
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(trace_module, "open", flaky_open, raising=False)

    data = store.read(RECORDING_ID)

    assert data["id"] == RECORDING_ID
    assert data["stages"] == []
    assert len(calls) == 3


def test_read_stops_after_the_last_retry(store, monkeypatch):
    store.start(RECORDING_ID, origin="kindle")
    calls = []

    def busy_open(*args, **kwargs):
        calls.append(args[0])
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(trace_module, "open", busy_open, raising=False)

    assert store.read(RECORDING_ID) is None
    assert len(calls) == trace_module._RETRY_ATTEMPTS


def test_list_recent_retries_a_transient_permission_error(store, monkeypatch):
    store.start(RECORDING_ID, origin="kindle")
    real_open = open
    calls = []

    def flaky_open(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 1:
            raise PermissionError(13, "sharing violation")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(trace_module, "open", flaky_open, raising=False)

    rows = store.list_recent()

    assert [row["id"] for row in rows] == [RECORDING_ID]
    assert len(calls) == 2


def test_an_unsafe_id_is_rejected(store):
    for value in ("", "..", "../escape", "a/b", "a\\b", "c:relative", "x\x00y"):
        with pytest.raises(ValueError):
            store.path(value)


def test_start_requires_an_origin(store):
    with pytest.raises(ValueError):
        store.start(RECORDING_ID, origin="")


# Logging -------------------------------------------------------------------


def test_log_stage_feeds_the_buffer(saved_logging):
    configure_logging("INFO")

    log_stage("receive", RECORDING_ID, duration_ms=3.5, outcome="ok", attempts=2)

    assert len(LOG_BUFFER) == 1
    line = LOG_BUFFER[-1]
    assert "stage=receive" in line
    assert f"id={RECORDING_ID}" in line
    assert "dur_ms=3.5" in line
    assert "outcome=ok" in line
    assert "attempts=2" in line


def test_configure_logging_installs_one_handler(saved_logging):
    LOG_BUFFER.clear()

    configure_logging("INFO")
    configure_logging("INFO")
    log_stage("receive", RECORDING_ID)

    assert len(LOG_BUFFER) == 1


def test_the_buffer_is_bounded(saved_logging):
    assert LOG_BUFFER.maxlen == LOG_BUFFER_SIZE == 500


def test_the_level_filter_applies(saved_logging):
    configure_logging("WARNING")

    log_stage("receive", RECORDING_ID)

    assert len(LOG_BUFFER) == 0

    configure_logging("INFO")
    log_stage("receive", RECORDING_ID)

    assert len(LOG_BUFFER) == 1


def test_configure_logging_reads_the_environment(saved_logging, monkeypatch):
    monkeypatch.setenv("KVB_LOG_LEVEL", "ERROR")

    configure_logging()

    assert logging.getLogger("voice_bridge_server").level == logging.ERROR


def test_log_stage_never_writes_prompt_text(saved_logging, caplog):
    caplog.set_level(logging.DEBUG)
    secret = "SECRET PROMPT TEXT"

    log_stage(
        "prompt",
        RECORDING_ID,
        duration_ms=1,
        detail={"user": secret},
        prompt=secret,
        passage=secret,
        text=secret,
        response_text=secret,
        question=secret,
        model="mistralai/mistral-small-2603",
    )

    assert secret not in caplog.text
    assert caplog.records
    record = caplog.records[-1]
    assert "stage=prompt" in record.getMessage()
    assert "model=mistralai/mistral-small-2603" in record.getMessage()
    # No message arguments are passed, so nothing is hidden in record.args.
    assert record.args in (None, ())


def test_log_stage_drops_non_scalar_fields(saved_logging, caplog):
    caplog.set_level(logging.DEBUG)

    log_stage("answer", RECORDING_ID, usage={"cost": 1}, stages=[{"name": "x"}])

    message = caplog.records[-1].getMessage()
    assert "usage" not in message
    assert "stages" not in message


def test_log_stage_keeps_one_line_per_record(saved_logging, caplog):
    caplog.set_level(logging.DEBUG)

    log_stage("answer", RECORDING_ID, note="a\nb\tc")

    assert caplog.records[-1].getMessage().count("\n") == 0
