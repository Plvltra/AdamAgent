"""成功回答的独立朗读记录；朗读失败不修改模型轮次，也不重新请求模型。"""

import io
import json
import re
import secrets
import wave
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from adam.audio.input import SpeechInput
from adam.audio.models import AudioBudget, Recording
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionSnapshot, Quantities


class SpeechBackend(Protocol):
    """可取消的有界合成与播放；play返回前必须释放本次设备。"""

    def synthesize(self, text: str, *, cancel: CancellationToken) -> bytes: ...
    def play(self, audio: bytes, *, cancel: CancellationToken) -> None: ...


class SpeechOutput:
    """每个成功轮次最多朗读一次；同轮次重试仅返回记录，不重合成或重播。"""

    def __init__(self, root: Path, backend: SpeechBackend, budget: AudioBudget) -> None:
        self.root, self.backend, self.budget = root, backend, budget

    def speak(
        self,
        session_id: str,
        turn_id: str,
        text: str,
        *,
        cancel: CancellationToken,
        playback_id: str | None = None,
    ) -> dict[str, Any]:
        """宿主传入已成功回答及可选播放身份；先占用再合成，独立保存失败/取消和时长。"""
        if any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) is None for item in (session_id, turn_id)
        ):
            raise ValueError("invalid speech source")
        if not text.strip() or len(text) > 1500:
            raise ValueError("speech text must contain 1..1500 characters")
        if playback_id is not None and re.fullmatch(r"[0-9a-f]{32}", playback_id) is None:
            raise ValueError("invalid playback identity")
        root = self.root / session_id
        path = root / (turn_id + ".json")
        digest = sha256(text.encode()).hexdigest()
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value["text_sha256"] != digest:
                raise ValueError("turn already bound to different speech text")
            return value
        cancel.check()
        root.mkdir(parents=True, exist_ok=True)
        value: dict[str, Any] = dict(
            session_id=session_id,
            turn_id=turn_id,
            playback_id=playback_id or secrets.token_hex(16),
            text_sha256=digest,
            model_state="succeeded",
            state="synthesizing",
            duration_ms=None,
            audio_sha256=None,
            error=None,
            cost=None,
        )
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
        started = monotonic()
        attempted = False
        accounted = False
        owner = "tts:" + value["playback_id"]
        try:
            with self.budget.reserve(Quantities(audio_ms=60000, calls=1)):
                cancel.check()
                attempted = True
                audio = self.backend.synthesize(text, cancel=cancel)
                with wave.open(io.BytesIO(audio), "rb") as wav:
                    duration = wav.getnframes() * 1000 // wav.getframerate()
                SpeechInput._validate(Recording(audio, duration, "TTS"))
                self.budget.update(
                    owner, ConsumptionSnapshot(Quantities(audio_ms=duration, calls=1))
                )
                accounted = True
                value.update(duration_ms=duration, audio_sha256=sha256(audio).hexdigest())
                path.with_suffix(".wav").write_bytes(audio)
                cancel.check()
                value.update(state="playing", playback_started_seconds=monotonic() - started)
                self._save(path, value)
                self.backend.play(audio, cancel=cancel)
                cancel.check()
                value["state"] = "played"
        except BaseException as error:
            if attempted and not accounted:
                self.budget.update(owner, ConsumptionSnapshot(Quantities(calls=1), ("audio_ms",)))
            value.update(
                state="cancelled"
                if cancel.cancelled or isinstance(error, KeyboardInterrupt)
                else "failed",
                error=type(error).__name__,
            )
            raise
        finally:
            value["elapsed_seconds"] = monotonic() - started
            self._save(path, value)
        return value

    @staticmethod
    def _save(path: Path, value: dict[str, Any]) -> None:
        """原子写独立朗读状态；原占用不删除，保存失败不能释放重复播放机会。"""
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
