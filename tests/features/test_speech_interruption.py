"""旧播报失效及停止顺序；真实原应用安全点消费，设备和模型边界显式模拟。"""

import json
from threading import Event

import pytest

from adam.audio.playback import SpeechPlayback
from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.contracts.models import Completion
from adam.session.storage import SessionStore
from tests.features.test_speech_output import Backend, output


def test_interrupt_releases_device_before_original_safe_point_submission(tmp_path):
    backend = Backend("wait")
    playback = SpeechPlayback(output(tmp_path / "speech", backend))
    lease = playback.begin("previous-session", "previous-turn")
    playback.start(lease, "已完成轮次的回答")
    assert backend.started.wait(3)
    observed, receipts = [], []

    class Model:
        def list_models(self) -> dict[str, str | None]:
            return {"test": None}

        def complete(self, messages, **kwargs):
            observed.append(messages)
            if len(observed) == 1:

                def submit():
                    assert backend.released
                    return app.submit_input("确认后的新语音文本", "capture-54")

                receipts.append(playback.interrupt(lease, submit))
                return Completion({"role": "assistant", "content": "过时回答"}, (1, 1))
            assert messages[-1] == {"role": "user", "content": "确认后的新语音文本"}
            return Completion({"role": "assistant", "content": "新回答"}, (1, 1))

    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    with SessionStore(config.sessions_dir) as store:
        app = build_application(config, store, Model(), tmp_path, live_input=True)
        try:
            assert app.send("原问题") == "新回答"
            assert len(observed) == 2 and receipts[0].sequence == 1
            records = list((store.root / "_inputs").glob("*/*.json"))
            record = json.loads(records[0].read_text(encoding="utf-8"))
            assert record["events"][0]["state"] == "in_context"
            assert record["events"][0]["event_id"] == "capture-54"
            with pytest.raises(ValueError, match="stale"):
                playback.start(lease, "迟到回复不能发声")
        finally:
            playback.close()
            app.close()


def test_new_turn_rejects_old_playback_and_duplicate_start(tmp_path):
    backend = Backend("wait")
    playback = SpeechPlayback(output(tmp_path, backend))
    old = playback.begin("session", "old")
    current = playback.begin("session", "new")
    try:
        with pytest.raises(ValueError, match="stale"):
            playback.start(old, "旧回答")
        playback.start(current, "新回答")
        assert backend.started.wait(3)
        record = json.loads((tmp_path / "session/new.json").read_text(encoding="utf-8"))
        assert record["playback_id"] == current.playback_id
        with pytest.raises(ValueError, match="already"):
            playback.start(current, "新回答")
        with pytest.raises(ValueError, match="stale"):
            playback.interrupt(old, lambda: pytest.fail("stale input"))
        assert not backend.released
    finally:
        playback.close()
    assert backend.released and backend.plays == 1


def test_stop_timeout_never_submits_input(tmp_path):
    release = Event()

    class Stuck(Backend):
        def play(self, audio, *, cancel):
            self.started.set()
            release.wait(3)
            self.released = True

    backend = Stuck()
    playback = SpeechPlayback(output(tmp_path, backend), stop_timeout=0.02)
    lease = playback.begin("session", "turn")
    playback.start(lease, "回答")
    assert backend.started.wait(3)
    try:
        with pytest.raises(TimeoutError, match="not submitted"):
            playback.interrupt(lease, lambda: pytest.fail("must not submit before release"))
        with pytest.raises(ValueError, match="stale"):
            playback.start(lease, "旧回答")
    finally:
        release.set()
        playback.close()
    assert backend.released


def test_submission_rejection_does_not_restart_speech(tmp_path):
    backend = Backend("wait")
    playback = SpeechPlayback(output(tmp_path, backend))
    lease = playback.begin("session", "turn")
    playback.start(lease, "回答")
    assert backend.started.wait(3)

    def reject():
        assert backend.released
        raise ValueError("injected closed input queue")

    try:
        with pytest.raises(ValueError, match="closed"):
            playback.interrupt(lease, reject)
        assert backend.plays == 1 and backend.released
        with pytest.raises(ValueError, match="stale"):
            playback.interrupt(lease, reject)
    finally:
        playback.close()
