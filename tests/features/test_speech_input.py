"""按键语音输入的真实WAV/草稿/计量与设备生命周期；模拟边界明确，不冒充麦克风质量。"""

import io
import json
import struct
import subprocess
import sys
import time
import wave
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from adam.application.subagents.unit_budget import UnitBudget
from adam.audio.adapters.microphone import Microphone
from adam.audio.adapters.whisper import WhisperASR
from adam.audio.input import SpeechInput
from adam.audio.models import Recording, Transcription
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import Quantities
from adam.contracts.errors import RequestInterrupted


def recording():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(struct.pack("<h", 1000) * 3200)
    return Recording(output.getvalue(), 200, "explicit fixture")


class ASR:
    """只替换转写边界；返回固定文字，不声称识别了真实设备。"""

    def __init__(self, action=None):
        self.calls, self.action = 0, action

    def transcribe(self, audio, *, cancel):
        self.calls += 1
        return self.action(cancel) if self.action else Transcription("原始转写", 5)


def service(root, backend):
    quota = UnitBudget(Quantities(output_tokens=2048, audio_ms=1000, calls=2))
    return SpeechInput(root, backend, quota), quota


def test_recording_persists_source_then_explicit_edit_confirmation(tmp_path):
    backend = ASR()
    speech, quota = service(tmp_path, backend)
    audio = recording()
    draft = speech.transcribe(audio, cancel=CancellationToken())
    assert draft.state == "pending_confirmation" and draft.confirmed_text is None
    assert (tmp_path / (draft.capture_id + ".wav")).read_bytes() == audio.wav
    assert draft.audio_version == sha256(audio.wav).hexdigest() and draft.device == audio.device
    confirmed = speech.confirm(draft.capture_id, "用户修正后的文字")
    assert speech.confirm(draft.capture_id, confirmed.confirmed_text) == confirmed
    assert confirmed.transcript == "原始转写" and backend.calls == 1
    saved = json.loads((tmp_path / (draft.capture_id + ".json")).read_text(encoding="utf-8"))
    assert saved["state"] == "confirmed" and saved["cost"] is None
    assert saved["usage"]["audio_ms"] == 200 and quota.usage().known.calls == 1
    with pytest.raises(ValueError):
        speech.confirm(draft.capture_id, "暗中换掉文字")
    assert not list(tmp_path.rglob("*.jsonl"))  # 尚未创建任何聊天轮次。


def test_discard_and_empty_transcript_never_confirm(tmp_path):
    speech, _ = service(tmp_path, ASR())
    draft = speech.transcribe(recording(), cancel=CancellationToken())
    assert speech.discard(draft.capture_id).state == "discarded"
    with pytest.raises(ValueError):
        speech.confirm(draft.capture_id, "仍要提交")
    empty, _ = service(tmp_path / "empty", ASR(lambda _: Transcription("", 0)))
    with pytest.raises(ValueError, match="usable"):
        empty.transcribe(recording(), cancel=CancellationToken())
    assert json.loads(next((tmp_path / "empty").glob("*.json")).read_text())["state"] == "failed"


def test_cancel_and_lost_result_preserve_actual_or_unknown_usage(tmp_path):
    def known(cancel):
        cancel.cancel()
        return Transcription("known", 3)

    speech, quota = service(tmp_path / "known", ASR(known))
    with pytest.raises(RequestInterrupted):
        speech.transcribe(recording(), cancel=CancellationToken())
    assert quota.usage().known.output_tokens == 3 and not quota.usage().unknown_units
    assert json.loads(next((tmp_path / "known").glob("*.json")).read_text())["state"] == "cancelled"

    def lost(cancel):
        raise TimeoutError("injected ASR response loss")

    backend = ASR(lost)
    unknown, quota = service(tmp_path / "unknown", backend)
    with pytest.raises(TimeoutError):
        unknown.transcribe(recording(), cancel=CancellationToken())
    assert backend.calls == 1 and quota.usage().unknown_units == ("output_tokens",)
    assert json.loads(next((tmp_path / "unknown").glob("*.json")).read_text())["state"] == "unknown"


def test_initial_save_failure_and_precancel_do_not_transcribe(tmp_path, monkeypatch):
    backend = ASR()
    speech, quota = service(tmp_path, backend)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RequestInterrupted):
        speech.transcribe(recording(), cancel=token)
    assert not list(tmp_path.iterdir())

    def fail(*args):
        raise OSError("injected atomic draft write failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        speech.transcribe(recording(), cancel=CancellationToken())
    assert backend.calls == 0 and quota.usage().known.calls == 0


@pytest.mark.parametrize("mode", ["normal", "silent", "missing", "timeout", "cancel"])
def test_microphone_closes_device_on_each_outcome(monkeypatch, mode):
    streams = []

    class Stream:
        def __init__(self, **args):
            self.callback, self.closed, self.aborted = args["callback"], False, False
            streams.append(self)

        def start(self):
            data = struct.pack("<h", 0 if mode == "silent" else 1000) * 1600
            self.callback(data, 1600, None, False)

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    def device(*args):
        if mode == "missing":
            raise OSError("injected missing input device")
        return {"max_input_channels": 1, "name": "fixture microphone", "default_samplerate": 16000}

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(query_devices=device, RawInputStream=Stream, CallbackAbort=RuntimeError),
    )
    mic = Microphone(max_seconds=0.1 if mode == "timeout" else 1)
    if mode == "missing":
        with pytest.raises(OSError):
            mic.start()
        assert not streams
        return
    mic.start()
    if mode == "cancel":
        mic.cancel()
        with pytest.raises(ValueError, match="active"):
            mic.finish()
    elif mode == "timeout":
        time.sleep(0.2)
        with pytest.raises(ValueError, match="duration"):
            mic.finish()
    elif mode == "silent":
        with pytest.raises(ValueError, match="silent"):
            mic.finish()
    else:
        assert mic.finish().duration_ms == 100
    assert streams[0].closed and streams[0].aborted


@pytest.mark.parametrize("cancelled", [True, False])
def test_asr_worker_cancel_and_timeout_reap_actual_process(tmp_path, monkeypatch, cancelled):
    (tmp_path / "model.bin").touch()
    original = subprocess.Popen
    processes = []
    token = CancellationToken()

    def start(args, **kwargs):
        # 推理边界替换成真实等待进程，只验证宿主取消回收，不冒充ASR成功。
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        if cancelled:
            token.cancel()
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    asr = WhisperASR(Path(sys.executable), tmp_path, timeout=0.1)
    with pytest.raises(RequestInterrupted if cancelled else TimeoutError):
        asr.transcribe(recording().wav, cancel=token)
    assert len(processes) == 1 and processes[0].poll() is not None
