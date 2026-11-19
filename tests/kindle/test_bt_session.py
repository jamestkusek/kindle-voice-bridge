"""Offline tests for the resident Bluetooth session daemon.

These tests use a fake supervisor, a fake Bumble controller, a fake uploader,
and a fake control channel. They do not open the UART, start Bluetooth, or
touch a device.
"""

import asyncio
import base64
import json
import os
import socket
import wave
import pytest

from kindle.voice_bridge import bt_session, uploader


class FakeSupervisor:
    class Interrupted(Exception):
        pass


class FakeCaptureFailure(Exception):
    pass


class FakeCaptureLocal:
    CaptureFailure = FakeCaptureFailure
    SAMPLE_RATE = 8000
    SAMPLE_WIDTH = 2
    UPLOAD_TIMEOUT = 30

    def __init__(self, directory):
        self.directory = directory
        self.written = []

    def write_wav(self, pcm):
        path = os.path.join(self.directory, "capture.wav")
        with open(path, "wb") as target:
            target.write(pcm)
        self.written.append(path)
        return path


class FakeUploader:
    class UploadError(Exception):
        pass

    def __init__(self):
        self.calls = []
        self.bodies = []
        self.contexts = []

    def upload_recording(self, path, base_url, token, timeout=30,
                         allow_insecure_lan=False, context_path=None):
        with open(path, "rb") as source:
            data = source.read()
        self.calls.append((path, base_url, token, len(data), allow_insecure_lan))
        self.bodies.append(data)
        self.contexts.append(context_path)
        return {"id": "recording-1"}


class FakeExperiment:
    def __init__(self):
        self.calls = []
        self.deadline = 100.0
        self.cleanup_failed = False
        self.latency_fd = None

    def prepare(self):
        self.calls.append("prepare")

    def handoff(self, budget=None):
        self.calls.append(("handoff", budget))
        self.acquire_latency()

    def open_uart(self, defer=False):
        self.calls.append(("open_uart", defer))

    def acquire_latency(self):
        self.calls.append("acquire_latency")
        self.latency_fd = object()

    def release_latency(self):
        self.calls.append("release_latency")
        self.latency_fd = None

    def cleanup(self):
        self.calls.append("cleanup")
        self.latency_fd = None


class FakeController:
    def __init__(self, record_result=None, record_gate=None):
        self.start_calls = 0
        self.record_calls = 0
        self.close_calls = 0
        self.connected = False
        self.record_result = record_result or (b"\x01\x00" * 4000, 0.5)
        self.record_gate = record_gate

    async def start(self):
        self.start_calls += 1
        self.connected = True

    async def record(self):
        self.record_calls += 1
        if self.record_gate is not None:
            await self.record_gate.wait()
        return self.record_result

    async def close(self):
        self.close_calls += 1
        self.connected = False


class PhaseController(FakeController):
    """A controller that reports the same phases as the Bumble controller."""

    def __init__(self):
        super().__init__()
        self.on_phase = None
        self.phases = []
        self.phase_gate = asyncio.Event()
        self.recording_gate = asyncio.Event()

    def _notify(self, phase):
        self.phases.append(phase)
        if self.on_phase is not None:
            self.on_phase(phase)

    async def record(self):
        self.record_calls += 1
        self._notify("preparing")
        await self.phase_gate.wait()
        self._notify("recording")
        await self.recording_gate.wait()
        return self.record_result


class ManualCapture:
    """The capture handle the real controller returns from start_recording."""

    def __init__(self, max_duration):
        self.stop = asyncio.Event()
        self.max_duration = max_duration
        self.max_duration_reached = False
        self.data = b""
        self.cue_offset = None
        self.cue_at = None
        self.audio_event = asyncio.Event()


class ManualController(FakeController):
    """A controller that supports the manual start/stop capture path."""

    def __init__(self, pcm=b"\x01\x00" * 4000):
        super().__init__()
        self.pcm = pcm
        self.start_recording_calls = 0
        self.stop_recording_calls = 0
        self.capture = None

    async def start_recording(self, max_duration=bt_session.MAX_MANUAL_RECORD_SECONDS):
        self.start_recording_calls += 1
        self.capture = ManualCapture(max_duration)
        return self.capture

    async def stop_recording(self):
        self.stop_recording_calls += 1
        return self.pcm, None


class FakeControl:
    """A fake control socket. It handles each command in its own task."""

    def __init__(self, session):
        self.session = session
        self.queue = asyncio.Queue()
        self.worker = None
        self.tasks = set()

    async def start(self):
        self.worker = asyncio.create_task(self._run())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _run(self):
        while True:
            command, future = await self.queue.get()
            task = asyncio.create_task(self._dispatch(command, future))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _dispatch(self, command, future):
        try:
            reply, stop_after = await self.session.dispatch(command)
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        if not future.done():
            future.set_result(reply)
        if stop_after:
            self.session.stop_event.set()

    async def send(self, command):
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((command, future))
        return await future


def build_session(tmp_path, controller, experiment=None, idle_timeout=600,
                  keep_latency=False, control_holder=None, fake_control=True):
    capture_local = FakeCaptureLocal(str(tmp_path))
    uploader = FakeUploader()
    experiment = experiment or FakeExperiment()

    def control_factory(session):
        control = FakeControl(session)
        if control_holder is not None:
            control_holder["control"] = control
        return control

    session = bt_session.Session(
        experiment=experiment,
        controller_factory=lambda: controller,
        config={"server_url": "http://127.0.0.1:8000", "token": "secret-token"},
        socket_path=str(tmp_path / "session.sock"),
        lock_path=str(tmp_path / "session.lock"),
        idle_timeout=idle_timeout,
        handoff_budget=210.0,
        upload_timeout=30,
        capture_local=capture_local,
        uploader=uploader,
        pw4_supervisor=FakeSupervisor,
        keep_latency=keep_latency,
        control_factory=control_factory if fake_control else None,
        state_path=str(tmp_path / "session.state"),
        command_path=str(tmp_path / "session.cmd"),
        context_path=str(tmp_path / "context.json"),
    )
    return session, capture_local, uploader, experiment


async def wait_for_control(holder, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while "control" not in holder:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("The fake control did not start.")
        await asyncio.sleep(0.01)
    return holder["control"]


def test_second_record_does_not_handoff(tmp_path):
    async def scenario():
        controller = FakeController()
        holder = {}
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, control_holder=holder)
        task = asyncio.create_task(bt_session.run_session(session))
        control = await wait_for_control(holder)
        first = await control.send("record")
        second = await control.send("record")
        status = await control.send("status")
        stop = await control.send("stop")
        await asyncio.wait_for(task, 5.0)
        return (first, second, status, stop, controller, capture_local,
                uploader, experiment)

    (first, second, status, stop, controller, capture_local,
     uploader, experiment) = asyncio.run(scenario())

    assert first.startswith("ok sent")
    assert second.startswith("ok sent")
    assert status == "ok idle connected"
    assert stop == "ok stopping"
    assert controller.start_calls == 1
    assert controller.record_calls == 2
    assert controller.close_calls == 1
    assert experiment.calls.count("prepare") == 1
    assert len([call for call in experiment.calls
                if isinstance(call, tuple) and call[0] == "handoff"]) == 1
    assert experiment.calls.count("cleanup") == 1
    assert len(uploader.calls) == 2
    assert len(capture_local.written) == 2
    assert all(not os.path.exists(call[0]) for call in uploader.calls)


def test_idle_timeout_stops_session(tmp_path):
    async def scenario():
        controller = FakeController()
        holder = {}
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, idle_timeout=0.2, control_holder=holder)
        task = asyncio.create_task(bt_session.run_session(session))
        await wait_for_control(holder)
        await asyncio.wait_for(task, 5.0)
        return controller, experiment

    controller, experiment = asyncio.run(scenario())

    assert controller.start_calls == 1
    assert controller.close_calls == 1
    assert experiment.calls.count("cleanup") == 1


def test_stop_during_record_cancels_record(tmp_path):
    async def scenario():
        gate = asyncio.Event()
        controller = FakeController(record_gate=gate)
        holder = {}
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, control_holder=holder)
        task = asyncio.create_task(bt_session.run_session(session))
        control = await wait_for_control(holder)
        record_task = asyncio.create_task(control.send("record"))
        while controller.record_calls == 0:
            await asyncio.sleep(0.01)
        stop_reply = await control.send("stop")
        record_reply = await asyncio.wait_for(record_task, 5.0)
        await asyncio.wait_for(task, 5.0)
        return stop_reply, record_reply, controller, experiment

    stop_reply, record_reply, controller, experiment = asyncio.run(scenario())

    assert stop_reply == "ok stopping"
    assert record_reply.startswith("error stopped")
    assert controller.close_calls == 1
    assert experiment.calls.count("cleanup") == 1


def test_keep_latency_keeps_the_hold(tmp_path):
    async def scenario():
        controller = FakeController()
        holder = {}
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, keep_latency=True, control_holder=holder)
        task = asyncio.create_task(bt_session.run_session(session))
        control = await wait_for_control(holder)
        await control.send("record")
        await control.send("stop")
        await asyncio.wait_for(task, 5.0)
        return experiment

    experiment = asyncio.run(scenario())

    assert experiment.calls.count("acquire_latency") == 1
    assert experiment.calls.count("release_latency") == 0


@pytest.mark.skipif(bt_session.fcntl is None, reason="flock is unavailable")
def test_second_session_is_refused_before_handoff(tmp_path):
    async def scenario():
        controller1 = FakeController()
        holder = {}
        session1, capture_local1, uploader1, experiment1 = build_session(
            tmp_path, controller1, control_holder=holder)
        task1 = asyncio.create_task(bt_session.run_session(session1))
        control1 = await wait_for_control(holder)

        controller2 = FakeController()
        experiment2 = FakeExperiment()
        session2, capture_local2, uploader2, experiment2 = build_session(
            tmp_path, controller2, experiment=experiment2)
        with pytest.raises(RuntimeError):
            await bt_session.run_session(session2)

        await control1.send("stop")
        await asyncio.wait_for(task1, 5.0)
        return controller2, experiment2

    controller2, experiment2 = asyncio.run(scenario())

    assert controller2.start_calls == 0
    assert "prepare" not in experiment2.calls
    assert not [call for call in experiment2.calls
                if isinstance(call, tuple) and call[0] == "handoff"]


def test_phase_command_reports_each_phase(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)

    async def scenario():
        idle = await session.dispatch("phase")
        session.recording = True
        session.set_phase("preparing")
        preparing = await session.dispatch("phase")
        session.set_phase("recording")
        recording = await session.dispatch("phase")
        session.set_phase("sending")
        sending = await session.dispatch("phase")
        session.stop_event.set()
        stopping = await session.dispatch("phase")
        return idle, preparing, recording, sending, stopping

    assert asyncio.run(scenario()) == (
        ("ok idle", False),
        ("ok preparing", False),
        ("ok recording", False),
        ("ok sending", False),
        ("ok stopping", False),
    )


def test_phase_moves_from_preparing_to_recording(tmp_path):
    async def scenario():
        controller = PhaseController()
        holder = {}
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, control_holder=holder)
        task = asyncio.create_task(bt_session.run_session(session))
        control = await wait_for_control(holder)
        record_task = asyncio.create_task(control.send("record"))
        while "preparing" not in controller.phases:
            await asyncio.sleep(0.01)
        preparing = await control.send("phase")
        controller.phase_gate.set()
        while "recording" not in controller.phases:
            await asyncio.sleep(0.01)
        recording = await control.send("phase")
        controller.recording_gate.set()
        record_reply = await record_task
        await control.send("stop")
        await asyncio.wait_for(task, 5.0)
        return preparing, recording, record_reply, controller

    preparing, recording, record_reply, controller = asyncio.run(scenario())

    assert preparing == "ok preparing"
    assert recording == "ok recording"
    assert record_reply.startswith("ok sent")
    assert controller.phases == ["preparing", "recording"]


def test_send_silence_is_off_by_default():
    args = bt_session.build_parser().parse_args([
        "--config", "upload.json",
        "--authorize-bluetooth-handoff",
        "--authorize-microphone-recording",
        "--confirm-bluetooth-enabled-in-ui",
    ])
    assert args.send_silence is False
    controller = bt_session.BumbleController(object(), {}, "AA:BB:CC:DD:EE:FF")
    assert controller.send_silence is False


def test_capture_send_silence_is_off_by_default():
    pytest.importorskip("bumble")
    pytest.importorskip("termios")
    from kindle.voice_bridge import capture_local

    args = capture_local.parse_args([
        "--config", "upload.json",
        "--authorize-bluetooth-handoff",
        "--authorize-microphone-recording",
        "--confirm-bluetooth-enabled-in-ui",
    ])
    assert args.send_silence is False


def test_client_reports_no_session(tmp_path, capsys):
    args = bt_session.build_parser().parse_args(
        ["--client", "status", "--socket", str(tmp_path / "missing.sock")])
    assert bt_session.client_main(args) == 1
    output = capsys.readouterr().out
    assert output.startswith("error ")


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name != "posix",
                    reason="AF_UNIX is unavailable")
def test_unix_control_socket(tmp_path):
    async def scenario():
        controller = FakeController()
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, fake_control=False)
        task = asyncio.create_task(bt_session.run_session(session))
        deadline = asyncio.get_running_loop().time() + 5.0
        while not os.path.exists(session.socket_path):
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("The control socket did not appear.")
            await asyncio.sleep(0.01)

        async def send(command):
            reader, writer = await asyncio.open_unix_connection(session.socket_path)
            writer.write((command + "\n").encode("ascii"))
            await writer.drain()
            line = await reader.readline()
            writer.close()
            await writer.wait_closed()
            return line.decode("ascii").strip()

        first = await send("record")
        second = await send("record")
        status = await send("status")
        stop = await send("stop")
        await asyncio.wait_for(task, 5.0)
        return first, second, status, stop, controller, experiment

    first, second, status, stop, controller, experiment = asyncio.run(scenario())

    assert first.startswith("ok sent")
    assert second.startswith("ok sent")
    assert status == "ok idle connected"
    assert stop == "ok stopping"
    assert controller.start_calls == 1
    assert controller.record_calls == 2
    assert experiment.calls.count("cleanup") == 1


def test_state_command_reports_idle_without_a_clock(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)

    async def scenario():
        idle = await session.dispatch("state")
        session.recording = True
        session.set_phase("preparing")
        preparing = await session.dispatch("state")
        return idle, preparing

    idle, preparing = asyncio.run(scenario())

    # A legacy one-shot capture has no known length, so both clocks are -1.
    assert idle == ("ok idle -1 -1 starting -", False)
    assert preparing == ("ok preparing -1 -1 starting -", False)


def test_state_command_counts_down_during_a_manual_recording(tmp_path):
    async def scenario():
        controller = ManualController()
        holder = {}
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller, control_holder=holder)
        task = asyncio.create_task(bt_session.run_session(session))
        control = await wait_for_control(holder)
        start = await control.send("start_recording")
        first = await control.send("state")
        await asyncio.sleep(0.2)
        second = await control.send("state")
        stop = await control.send("stop_recording")
        after = await control.send("state")
        await control.send("stop")
        await asyncio.wait_for(task, 5.0)
        return start, first, second, stop, after, controller

    start, first, second, stop, after, controller = asyncio.run(scenario())

    assert start == "ok recording"
    assert controller.start_recording_calls == 1
    assert controller.stop_recording_calls == 1

    fields = first.split(" ")
    assert fields[0:2] == ["ok", "recording"]
    assert fields[4] == "connected"
    assert fields[5] == "-"
    elapsed_first, remaining_first = int(fields[2]), int(fields[3])
    assert 0 <= elapsed_first < 15000
    assert 0 < remaining_first <= 15000
    assert elapsed_first + remaining_first == 15000

    elapsed_second, remaining_second = (int(part) for part in second.split(" ")[2:4])
    assert elapsed_second > elapsed_first
    assert remaining_second < remaining_first

    assert stop.startswith("ok sent")

    after_fields = after.split(" ")
    assert after_fields[0:2] == ["ok", "idle"]
    assert after_fields[2:4] == ["-1", "-1"]
    assert " ".join(after_fields[5:]) != "-"


def test_state_file_is_written_and_removed(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    path = tmp_path / "session.state"

    session.write_state_file()
    first = path.read_text(encoding="ascii").splitlines()[0]
    session.write_state_file()
    second = path.read_text(encoding="ascii").splitlines()[0]

    first_fields = first.split(" ", 6)
    second_fields = second.split(" ", 6)
    assert first_fields[0] == "v1"
    assert first_fields[2:] == ["idle", "-1", "-1", "starting", "-"]
    assert int(second_fields[1]) == int(first_fields[1]) + 1
    assert not (tmp_path / "session.state.tmp").exists()

    session.remove_state_file()
    assert not path.exists()


def test_state_file_carries_the_last_result(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    path = tmp_path / "session.state"

    session.last_record_result = "ok sent rec_1 5.00s"
    session.write_state_file()

    fields = path.read_text(encoding="ascii").splitlines()[0].split(" ", 6)
    assert fields[2:] == ["idle", "-1", "-1", "starting", "ok sent rec_1 5.00s"]


def test_command_file_stops_a_manual_recording(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    command_path = tmp_path / "session.cmd"
    event = asyncio.Event()
    session.recording = True
    session.record_kind = "manual"
    session.manual_stop_event = event

    command_path.write_text("stop_recording\n", encoding="ascii")
    session.poll_command_file()

    assert event.is_set()
    assert not command_path.exists()


def test_command_file_is_ignored_when_no_recording_runs(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    command_path = tmp_path / "session.cmd"
    event = asyncio.Event()
    session.recording = False
    session.manual_stop_event = event

    command_path.write_text("stop_recording\n", encoding="ascii")
    session.poll_command_file()

    assert not event.is_set()
    assert not command_path.exists()


def test_an_unknown_command_is_discarded(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    command_path = tmp_path / "session.cmd"
    event = asyncio.Event()
    session.recording = True
    session.manual_stop_event = event

    command_path.write_text("reboot\n", encoding="ascii")
    session.poll_command_file()

    assert not event.is_set()
    assert not command_path.exists()


def test_a_missing_command_file_is_harmless(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)

    session.poll_command_file()

    assert not (tmp_path / "session.cmd").exists()


def test_fetch_answer_publishes_a_ready_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(bt_session, "ANSWER_POLL_SECONDS", 0.01)
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    replies = [{"status": "pending"}, {"status": "ready", "text": "  It is a cloud.  "}]
    session._get_answer = lambda url: replies.pop(0)

    asyncio.run(session.fetch_answer("rec_test"))

    assert session.last_answer == "It is a cloud."
    state = (tmp_path / "session.state").read_text(encoding="ascii")
    assert "answer It is a cloud." in state


def test_fetch_answer_stops_on_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bt_session, "ANSWER_POLL_SECONDS", 0.01)
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    session._get_answer = lambda url: {"status": "failed", "error": "no answer"}

    asyncio.run(session.fetch_answer("rec_test"))

    assert session.last_answer is None


def test_fetch_answer_survives_a_network_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bt_session, "ANSWER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(bt_session, "ANSWER_TIMEOUT_SECONDS", 0.05)
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)

    def boom(url):
        raise OSError("connection refused")

    session._get_answer = boom

    asyncio.run(session.fetch_answer("rec_test"))

    assert session.last_answer is None


def test_the_state_file_has_an_answer_line(tmp_path):
    controller = FakeController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)

    session.write_state_file()
    lines = (tmp_path / "session.state").read_text(encoding="ascii").splitlines()
    assert lines[1] == "answer -"

    session.last_answer = "A short answer."
    session.write_state_file()
    lines = (tmp_path / "session.state").read_text(encoding="ascii").splitlines()
    assert lines[1] == "answer A short answer."


def test_command_file_starts_a_manual_recording(tmp_path):
    controller = ManualController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    session.controller = controller
    command_path = tmp_path / "session.cmd"
    command_path.write_text("start_recording\n", encoding="ascii")

    async def scenario():
        session.poll_command_file()
        await asyncio.sleep(0.05)
        return session.recording, session.record_kind, session.record_task

    recording, kind, task = asyncio.run(scenario())

    assert recording is True
    assert kind == "manual"
    assert task is not None
    assert not command_path.exists()


def test_command_file_does_not_start_a_second_recording(tmp_path):
    controller = ManualController()
    session, capture_local, uploader, experiment = build_session(tmp_path, controller)
    command_path = tmp_path / "session.cmd"
    session.recording = True
    session.record_kind = "manual"
    command_path.write_text("start_recording\n", encoding="ascii")

    async def scenario():
        session.poll_command_file()
        await asyncio.sleep(0.05)
        return session.record_task

    assert asyncio.run(scenario()) is None


def test_failed_upload_keeps_the_recording(tmp_path):
    """A failed upload must not delete the only copy of the audio."""

    async def scenario():
        controller = FakeController()
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller)

        def fail(path, base_url, token, timeout=30, allow_insecure_lan=False,
                 context_path=None):
            raise FakeUploader.UploadError("the network dropped")

        uploader.upload_recording = fail
        with pytest.raises(uploader.UploadError):
            await session._upload_pcm(b"\x01\x00" * 100, 0.0, False, False, None)
        return list(capture_local.written)

    written = asyncio.run(scenario())
    assert written, "the capture wrote no WAV"
    kept = [path[:-len(".wav")] + ".failed.wav" for path in written]
    assert any(os.path.exists(path) for path in kept), (
        "the failed upload deleted the audio instead of keeping it")


def test_begin_manual_recording_cancels_the_previous_answer_poll(tmp_path):
    """A stale answer must not be published against the next recording."""

    async def scenario():
        controller = FakeController()
        session, capture_local, uploader, experiment = build_session(
            tmp_path, controller)

        gate = asyncio.Event()

        async def slow_fetch(recording_id):
            await gate.wait()
            session.last_answer = "the answer to the first recording"

        session.fetch_answer = slow_fetch
        session.start_answer_poll("rec-1")
        await asyncio.sleep(0)
        first = session.answer_task

        session._begin_manual_recording()
        assert first.cancelling() > 0, "the in-flight poll was not cancelled"
        assert session.last_answer is None

        # The recording task this started is not under test; stop it before it
        # runs and then release the stale poll to prove it cannot publish.
        if session.record_task is not None:
            session.record_task.cancel()
            await asyncio.gather(session.record_task, return_exceptions=True)
        gate.set()
        await asyncio.sleep(0.05)
        assert session.last_answer is None

    asyncio.run(scenario())


# Context file, cue delay, and UTF-8 answers.


class CueController(ManualController):
    """A manual controller that trims at the cue like the real controller."""

    def __init__(self, pre_cue, post_cue, cue_delay):
        super().__init__()
        self.pre_cue = pre_cue
        self.post_cue = post_cue
        self.cue_delay = cue_delay
        self.uploaded = None

    async def start_recording(self, max_duration=bt_session.MAX_MANUAL_RECORD_SECONDS):
        self.start_recording_calls += 1
        self.capture = ManualCapture(max_duration)
        self.capture.data = bytearray(self.pre_cue)
        if self.cue_delay:
            await asyncio.sleep(self.cue_delay)
            self.capture.cue_offset = bt_session.cue_trim_offset(
                len(self.capture.data), FakeCaptureLocal.SAMPLE_RATE,
                FakeCaptureLocal.SAMPLE_WIDTH)
        return self.capture

    async def stop_recording(self):
        self.stop_recording_calls += 1
        data = self.capture.data + self.post_cue
        offset = self.capture.cue_offset or 0
        self.uploaded = bytes(data[offset:])
        return self.uploaded, None


async def run_one_command_recording(session, controller, tmp_path, settle=0.1):
    """Start one capture from the command file and stop it."""
    session.controller = controller
    command_path = tmp_path / "session.cmd"
    command_path.write_text("start_recording\n", encoding="ascii")
    session.poll_command_file()
    while not session.recording:
        await asyncio.sleep(0.01)
    await asyncio.sleep(settle)
    reply, _stop_after = await session.dispatch("stop_recording")
    return reply


def test_the_context_file_is_read_once_and_removed(tmp_path, capsys):
    payload = b'{"v": 1, "book": {"title": "Dune"}}'
    controller = ManualController()
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)
    context_file = tmp_path / "context.json"
    context_file.write_bytes(payload)

    async def scenario():
        reply = await run_one_command_recording(session, controller, tmp_path)
        return reply, session.context_bytes

    reply, kept = asyncio.run(scenario())

    assert reply.startswith("ok sent")
    assert kept is None, "the context must not outlive the recording"
    assert fake_uploader.contexts == [payload]
    assert not context_file.exists()
    out = capsys.readouterr().out
    assert f"Context: {len(payload)} bytes." in out
    assert payload.decode("ascii") not in out, "the payload reached the log"


def test_a_missing_context_file_uploads_without_a_header(tmp_path, capsys):
    controller = ManualController()
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)

    async def scenario():
        return await run_one_command_recording(session, controller, tmp_path)

    reply = asyncio.run(scenario())

    assert reply.startswith("ok sent")
    assert fake_uploader.contexts == [None]
    assert "Context: 0 bytes." in capsys.readouterr().out


def test_an_unreadable_context_file_uploads_without_a_header(tmp_path):
    controller = ManualController()
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)
    # A directory in place of the file cannot be read.
    os.makedirs(session.context_path)

    async def scenario():
        return await run_one_command_recording(session, controller, tmp_path)

    reply = asyncio.run(scenario())

    assert reply.startswith("ok sent")
    assert fake_uploader.contexts == [None]


def test_cue_trim_offset_keeps_a_pre_roll():
    assert bt_session.cue_trim_offset(0, 8000, 2) == 0
    assert bt_session.cue_trim_offset(1000, 8000, 2) == 0
    # 0.2 s at 8 kHz mono 16-bit is 3200 bytes.
    assert bt_session.cue_trim_offset(10000, 8000, 2) == 6800
    assert bt_session.cue_trim_offset(10001, 8000, 2) == 6800


def test_the_cue_delay_trims_the_start_of_the_upload(tmp_path):
    pre_cue = b"\x00\x00" * 8000
    post_cue = b"\x01\x00" * 4000
    controller = CueController(pre_cue, post_cue, cue_delay=0.02)
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)

    async def scenario():
        return await run_one_command_recording(session, controller, tmp_path)

    reply = asyncio.run(scenario())

    preroll = bt_session.cue_trim_offset(len(pre_cue), 8000, 2)
    assert preroll == len(pre_cue) - 3200
    assert reply.startswith("ok sent")
    assert controller.uploaded == pre_cue[preroll:] + post_cue
    assert fake_uploader.bodies == [pre_cue[preroll:] + post_cue]


def test_a_zero_cue_delay_keeps_the_whole_capture(tmp_path):
    pre_cue = b"\x00\x00" * 8000
    post_cue = b"\x01\x00" * 4000
    controller = CueController(pre_cue, post_cue, cue_delay=0)
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)

    async def scenario():
        return await run_one_command_recording(session, controller, tmp_path)

    reply = asyncio.run(scenario())

    assert reply.startswith("ok sent")
    assert controller.capture.cue_offset is None
    assert fake_uploader.bodies == [pre_cue + post_cue]


class SlowStartController(ManualController):
    """A manual controller that spends time cueing before it returns."""

    async def start_recording(self, max_duration=bt_session.MAX_MANUAL_RECORD_SECONDS):
        self.start_recording_calls += 1
        await asyncio.sleep(0.2)
        self.capture = ManualCapture(max_duration)
        return self.capture


def test_the_capture_budget_starts_at_the_cue(tmp_path):
    controller = SlowStartController()
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)

    async def scenario():
        session.controller = controller
        command_path = tmp_path / "session.cmd"
        command_path.write_text("start_recording\n", encoding="ascii")
        session.poll_command_file()
        # The cue is the moment the phase becomes recording.
        while session.phase != "recording":
            await asyncio.sleep(0.01)
        state = await session.dispatch("state")
        return state[0]

    fields = asyncio.run(scenario()).split(" ")

    assert fields[0:2] == ["ok", "recording"]
    # The cue took 0.2 s, and none of it counts against the capture.
    assert int(fields[2]) < 200
    assert int(fields[3]) > 14800


def test_delay_cue_offsets_and_logs_once(tmp_path, capsys):
    capture_local = FakeCaptureLocal(str(tmp_path))
    controller = bt_session.BumbleController(
        capture_local, {}, "AA:BB:CC:DD:EE:FF", cue_delay=0.01)
    capture = ManualCapture(15.0)
    capture.data = bytearray(b"\x00\x00" * 8000)
    capture.audio_event = asyncio.Event()

    asyncio.run(controller._delay_cue(capture))

    assert capture.cue_offset == len(capture.data) - 3200
    assert capture.cue_at is not None
    assert capsys.readouterr().out.count("Audio: cue delayed 0.01 s.") == 1


def test_a_negative_cue_delay_is_rejected():
    with pytest.raises(ValueError):
        bt_session.BumbleController(
            FakeCaptureLocal("."), {}, "AA:BB:CC:DD:EE:FF", cue_delay=-1)


def test_context_file_and_cue_delay_defaults():
    args = bt_session.build_parser().parse_args([
        "--config", "upload.json",
        "--authorize-bluetooth-handoff",
        "--authorize-microphone-recording",
        "--confirm-bluetooth-enabled-in-ui",
    ])
    assert args.context_file == "/tmp/voicebridge-context.json"
    assert args.cue_delay == 2.0


def test_a_negative_cue_delay_is_a_parser_error():
    with pytest.raises(SystemExit):
        bt_session.build_parser().parse_args([
            "--config", "upload.json",
            "--cue-delay", "-1",
            "--authorize-bluetooth-handoff",
            "--authorize-microphone-recording",
            "--confirm-bluetooth-enabled-in-ui",
        ])


def test_answer_text_keeps_utf8_and_drops_controls():
    answer = "Caf\u00e9 \u2014 \u201cquoted\u201d\nsecond\tline\x00\x07"
    text = bt_session.answer_text(answer)
    assert text == "Caf\u00e9 \u2014 \u201cquoted\u201d second line"
    assert "\x00" not in text and "\x07" not in text
    assert "\n" not in text


def test_answer_text_truncates_by_characters():
    text = bt_session.answer_text("\u00e9" * 600, limit=480)
    assert len(text) == 480
    assert text.endswith("...")
    assert bt_session.answer_text("   ", limit=10) == "-"


def test_the_answer_body_keeps_utf8(tmp_path):
    controller = FakeController()
    session, capture_local, fake_uploader, experiment = build_session(
        tmp_path, controller)
    session.last_answer = "L\u2019\u00e9t\u00e9 \u2014 \u201cvoil\u00e0\u201d"

    body = session.answer_body()
    assert body == "L\u2019\u00e9t\u00e9 \u2014 \u201cvoil\u00e0\u201d"

    session.write_state_file()
    lines = (tmp_path / "session.state").read_text(encoding="utf-8").splitlines()
    assert lines[1] == "answer L\u2019\u00e9t\u00e9 \u2014 \u201cvoil\u00e0\u201d"


def test_safe_text_stays_ascii():
    text = bt_session.safe_text("Caf\u00e9 \u2014 ok")
    assert text == "Caf ok"
    assert all(ord(char) < 127 for char in text)


def write_wav_file(path, samples=400):
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(b"\x01\x00" * samples)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def getcode(self):
        return 201

    def read(self, size=-1):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse({"id": "rec-1"})


def request_header(request, name):
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def open_fake_opener(monkeypatch):
    opener = FakeOpener()
    monkeypatch.setattr(uploader, "build_opener", lambda *args: opener)
    return opener


def test_upload_without_a_context_sends_no_header(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    write_wav_file(path)
    opener = open_fake_opener(monkeypatch)

    result = uploader.upload_recording(
        str(path), "http://127.0.0.1:8000", "token")

    assert result == {"id": "rec-1"}
    request = opener.requests[0]
    assert request_header(request, "X-KVB-Context") is None
    assert request_header(request, "Content-Length") == str(path.stat().st_size)


def test_upload_with_a_context_sends_the_header(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    write_wav_file(path)
    context = '{"v": 1, "book": {"title": "Caf\u00e9"}}'.encode("utf-8")
    context_path = tmp_path / "context.json"
    context_path.write_bytes(context)
    opener = open_fake_opener(monkeypatch)

    uploader.upload_recording(
        str(path), "http://127.0.0.1:8000", "token",
        context_path=str(context_path))

    request = opener.requests[0]
    value = request_header(request, "X-KVB-Context")
    assert "=" not in value
    padded = value + "=" * (-len(value) % 4)
    assert base64.urlsafe_b64decode(padded.encode("ascii")) == context
    # The body and its length are unchanged by the header.
    assert request_header(request, "Content-Length") == str(path.stat().st_size)
    assert request.data == path.read_bytes()


def test_upload_with_an_oversized_context_is_rejected_before_sending(
        tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    write_wav_file(path)
    context_path = tmp_path / "context.json"
    context_path.write_bytes(b"x" * 4000)
    opener = open_fake_opener(monkeypatch)

    with pytest.raises(uploader.UploadError):
        uploader.upload_recording(
            str(path), "http://127.0.0.1:8000", "token",
            context_path=str(context_path))

    assert opener.requests == []


def test_encode_context_limits_the_encoded_size():
    # 3072 bytes encode to exactly 4096 characters without padding.
    assert len(uploader.encode_context(b"x" * 3072)) == 4096
    with pytest.raises(uploader.UploadError):
        uploader.encode_context(b"x" * 3073)


def test_upload_ignores_a_missing_context_file(tmp_path, monkeypatch):
    path = tmp_path / "sample.wav"
    write_wav_file(path)
    opener = open_fake_opener(monkeypatch)

    uploader.upload_recording(
        str(path), "http://127.0.0.1:8000", "token",
        context_path=str(tmp_path / "absent.json"))

    assert request_header(opener.requests[0], "X-KVB-Context") is None
