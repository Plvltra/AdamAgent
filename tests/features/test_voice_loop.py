"""按键录音宿主走真实应用/安全点/日志；仅替换录音、ASR、播放和模型边界。"""

import json
from functools import partial
from threading import Event, get_ident

import pytest

from adam.application.subagents.unit_budget import UnitBudget
from adam.application.voice import VoiceConversation
from adam.application.voice_loop import VoiceLoop
from adam.audio.input import SpeechInput, load_confirmed
from adam.audio.output import SpeechOutput
from adam.audio.playback import SpeechPlayback
from adam.bootstrap import build_application
from adam.config.settings import AppConfig, LLMConfig, ReflectionConfig, ToolDiscoveryConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import Quantities
from adam.contracts.models import Completion
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore
from tests.features.test_speech_input import ASR, recording
from tests.features.test_speech_output import Backend


@pytest.mark.parametrize("during_model", [True, False])
def test_two_captures_use_safe_point_or_next_turn_and_stop_playback(tmp_path, during_model):
    main_thread = get_ident()
    model_ready, supplemented, finished = Event(), Event(), Event()
    backend = Backend("wait")
    spoken, observed, captures, notices = [], [], [], []
    read_count = 0

    class Microphone:
        def start(self):
            if captures and not during_model:
                assert backend.released, "新录音必须在旧播报释放设备后开始"
            captures.append(True)

        def finish(self):
            return recording()

        def cancel(self):
            pass

    class Model:
        def list_models(self) -> dict[str, str | None]:
            return {"test": None}

        def complete(self, messages, **kwargs):
            assert get_ident() == main_thread
            observed.append(messages)
            if len(observed) == 1:
                model_ready.set()
                if during_model:
                    assert supplemented.wait(5)
                    return Completion({"role": "assistant", "content": "旧答案"}, (1, 1))
            return Completion({"role": "assistant", "content": "回答"}, (1, 1))

    def read(prompt):
        nonlocal read_count
        read_count += 1
        if read_count == 4:
            assert (model_ready if during_model else backend.started).wait(5)
        if read_count == 6:
            return "第二段确认文字"
        if read_count == 7:
            assert finished.wait(5)
            return "/quit"
        return ""

    def write(text):
        notices.append(text)
        if text.startswith("补充已接受"):
            supplemented.set()
        if text == "回答":
            spoken.append(text)
            if during_model or len(spoken) == 2:
                finished.set()

    config = AppConfig(
        LLMConfig("https://unused.test", "test", "secret", stream=False),
        tmp_path / "sessions",
        "system",
        reflection=ReflectionConfig(enabled=False),
        tool_discovery=ToolDiscoveryConfig(mode="full"),
    )
    cancel, conversations = CancellationToken(), []
    with SessionStore(config.sessions_dir) as store:
        app = build_application(
            config,
            store,
            Model(),
            tmp_path,
            live_input=True,
            cancel=cancel,
            observe_conversation=conversations.append,
        )
        try:
            voice = VoiceConversation(
                app,
                conversations[0],
                store,
                SessionMedia(store, MediaStore(store.root / "_media")),
                partial(load_confirmed, tmp_path / "drafts"),
            )
            speech = SpeechInput(
                tmp_path / "drafts",
                ASR(),
                UnitBudget(Quantities(output_tokens=2048, audio_ms=1000, calls=2)),
            )
            playback = SpeechPlayback(
                SpeechOutput(
                    tmp_path / "speech", backend, UnitBudget(Quantities(audio_ms=120000, calls=2))
                )
            )
            host = VoiceLoop(voice, speech, Microphone(), playback, cancel, read, write)
            host.run()
            assert host._thread is not None and not host._thread.is_alive()
            assert len(captures) == 2 and len(observed) == 2
            assert observed[-1][-1] == {"role": "user", "content": "第二段确认文字"}
            assert not any("未完成" in text for text in notices)
            assert app.session is not None
            turns = store.load_turns(app.session.session_id)
            assert len(turns) == (1 if during_model else 2)
            if during_model:
                paths = list((store.root / "_media/voice_supplements").glob("*.json"))
                saved = json.loads(paths[0].read_text(encoding="utf-8"))
                assert saved["state"] == "accepted" and saved["turn_id"] == turns[0].turn_id
                assert (tmp_path / "drafts" / (saved["capture_id"] + ".wav")).exists()
                original = json.loads(
                    next((store.root / "_inputs").glob("*/*.json")).read_text(encoding="utf-8")
                )
                assert original["events"][0]["state"] == "in_context"
            else:
                assert backend.released
        finally:
            app.close()
