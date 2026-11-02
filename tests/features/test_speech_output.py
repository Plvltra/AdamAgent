"""朗读实际保存WAV/独立回执；设备边界注入，模型历史经原应用验证。"""

import json
from threading import Event, Thread

import httpx
import pytest

from adam.application.subagents.unit_budget import UnitBudget
from adam.audio.output import SpeechOutput
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import Quantities
from adam.contracts.errors import RequestInterrupted
from tests.features.test_speech_input import recording
from tests.features.test_tool_loop import answer
from tests.features.test_voice_conversation import application, confirmed


class Backend:
    """仅模拟音频设备边界；可注入合成失败、播放失败或等待取消。"""

    def __init__(self, failure=None):
        self.failure = failure
        self.synthesis = self.plays = 0
        self.started = Event()
        self.released = False

    def synthesize(self, text, *, cancel):
        self.synthesis += 1
        if self.failure == "synthesize":
            raise RuntimeError("injected synthesis failure")
        return recording().wav

    def play(self, audio, *, cancel):
        self.plays += 1
        self.started.set()
        try:
            if self.failure == "play":
                raise OSError("injected device failure")
            if self.failure == "wait":
                while True:
                    cancel.wait(0.02)
        finally:
            self.released = True


def output(root, backend):
    return SpeechOutput(root, backend, UnitBudget(Quantities(audio_ms=60000, calls=1)))


def test_success_wav_and_restart_do_not_replay(tmp_path):
    backend = Backend()
    service = output(tmp_path, backend)
    value = service.speak("session", "turn", "回答", cancel=CancellationToken())
    assert value["state"] == "played" and value["model_state"] == "succeeded"
    assert backend.released and backend.plays == 1
    assert (tmp_path / "session/turn.wav").read_bytes() == recording().wav
    assert (
        output(tmp_path, backend).speak("session", "turn", "回答", cancel=CancellationToken())
        == value
    )
    assert backend.synthesis == backend.plays == 1
    with pytest.raises(ValueError, match="different"):
        service.speak("session", "turn", "不同回答", cancel=CancellationToken())


@pytest.mark.parametrize("failure", ["synthesize", "play"])
def test_speech_failure_keeps_original_model_success_and_never_resends(tmp_path, failure):
    capture = confirmed(tmp_path / "drafts")
    with application(tmp_path, lambda *args: httpx.Response(200, json=answer("回答"))) as (
        voice,
        app,
        store,
        bodies,
    ):
        result = voice.send(capture)
        backend = Backend(failure)
        service = output(tmp_path / "speech", backend)
        with pytest.raises((RuntimeError, OSError)):
            service.speak(
                result["session_id"], result["turn_id"], result["reply"], cancel=CancellationToken()
            )
        value = service.speak(
            result["session_id"], result["turn_id"], result["reply"], cancel=CancellationToken()
        )
        assert value["state"] == "failed" and value["model_state"] == "succeeded"
        assert voice.send(capture)["state"] == "succeeded" and len(bodies) == 1
        assert app.session is not None
        assert len(store.load_turns(app.session.session_id)) == 1
        assert backend.synthesis == 1


def test_cancel_waits_for_device_release_and_does_not_replay(tmp_path):
    backend, cancel, errors = Backend("wait"), CancellationToken(), []
    service = output(tmp_path, backend)

    def run():
        try:
            service.speak("session", "turn", "回答", cancel=cancel)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=run)
    worker.start()
    assert backend.started.wait(3)
    cancel.cancel()
    worker.join(3)
    assert not worker.is_alive() and backend.released
    assert len(errors) == 1 and isinstance(errors[0], RequestInterrupted)
    value = json.loads((tmp_path / "session/turn.json").read_text(encoding="utf-8"))
    assert value["state"] == "cancelled" and value["duration_ms"] > 0
    service.speak("session", "turn", "回答", cancel=CancellationToken())
    assert backend.plays == 1


def test_initial_record_failure_prevents_synthesis(tmp_path):
    backend = Backend()
    root = tmp_path / "unavailable"
    root.write_text("file", encoding="utf-8")
    with pytest.raises(OSError):
        output(root, backend).speak("session", "turn", "回答", cancel=CancellationToken())
    assert backend.synthesis == backend.plays == 0
