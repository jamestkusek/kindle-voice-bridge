"""Offline tests for the resident Bluetooth session daemon.

These tests use a fake supervisor, a fake Bumble controller, a fake uploader,
and a fake control channel. They do not open the UART, start Bluetooth, or
touch a device.
"""

import asyncio
import os
import socket
import pytest

from kindle.voice_bridge import bt_session


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

    def upload_recording(self, path, base_url, token, timeout=30,
                         allow_insecure_lan=False):
        with open(path, "rb") as source:
            data = source.read()
        self.calls.append((path, base_url, token, len(data), allow_insecure_lan))
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
