"""录音证据与可确认/编辑草稿；该阶段不创建Conversation轮次。"""

import io
import json
import secrets
import wave
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from adam.audio.models import AudioBudget, Recording, Transcriber
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionSnapshot, Quantities


@dataclass(frozen=True)
class SpeechDraft:
    """固定录音来源和原始转写；confirmed_text只有显式确认后才有值。"""

    capture_id: str
    device: str
    created_at: str
    audio_version: str
    duration_ms: int
    state: str
    transcript: str = ""
    confirmed_text: str | None = None
    error: str | None = None
    usage: Quantities | None = None
    cost: str | None = None


class SpeechInput:
    """先保存真实WAV再尝试转写；失败不重做，确认只修改草稿不发送模型。"""

    def __init__(self, root: Path, asr: Transcriber, budget: AudioBudget) -> None:
        self.root, self.asr, self.budget = root, asr, budget
        self._drafts: dict[str, SpeechDraft] = {}

    def transcribe(self, recording: Recording, *, cancel: CancellationToken) -> SpeechDraft:
        """有界完整录音转为待确认草稿；静音/空识别、取消和失败都不能变成用户请求。"""
        cancel.check()
        self._validate(recording)
        if len(self._drafts) >= 64:
            raise ValueError("speech draft capacity exceeded")
        identity = secrets.token_hex(16)
        draft = SpeechDraft(
            identity,
            recording.device,
            datetime.now(UTC).isoformat(),
            sha256(recording.wav).hexdigest(),
            recording.duration_ms,
            "transcribing",
        )
        owner = "transcription:" + identity
        with self.budget.reserve(
            Quantities(output_tokens=1024, audio_ms=recording.duration_ms, calls=1)
        ):
            self.root.mkdir(parents=True, exist_ok=True)
            audio = self.root / (identity + ".wav")
            with audio.open("xb") as output:
                output.write(recording.wav)
            self._save(draft)
            attempted = False
            try:
                cancel.check()
                attempted = True
                result = self.asr.transcribe(recording.wav, cancel=cancel)
            except BaseException as error:
                # 无可靠后端回执时保留未知输出；已经开始的转写不作为零消费重新准入。
                self.budget.update(
                    owner,
                    ConsumptionSnapshot(
                        Quantities(audio_ms=recording.duration_ms, calls=1)
                        if attempted
                        else Quantities(),
                        ("output_tokens",) if attempted else (),
                    ),
                )
                draft = replace(
                    draft,
                    state="unknown" if attempted else "cancelled",
                    error=type(error).__name__,
                    usage=Quantities(output_tokens=None, audio_ms=recording.duration_ms, calls=1)
                    if attempted
                    else Quantities(),
                )
                try:
                    self._save(draft)
                except Exception:
                    error.add_note("语音草稿失败状态保存失败，原录音保留；不要自动重新转写。")
                raise
            self.budget.update(
                owner,
                ConsumptionSnapshot(
                    Quantities(
                        output_tokens=result.output_tokens or 0,
                        audio_ms=recording.duration_ms,
                        calls=1,
                    ),
                    ("output_tokens",) if result.output_tokens is None else (),
                ),
            )
            draft = replace(
                draft,
                usage=Quantities(
                    output_tokens=result.output_tokens, audio_ms=recording.duration_ms, calls=1
                ),
            )
            if cancel.cancelled:
                self._save(replace(draft, state="cancelled", error="cancelled_after_asr"))
                cancel.check()
            if not result.text.strip() or len(result.text) > 8000:
                draft = replace(draft, state="failed", error="empty_or_oversized_transcript")
                self._save(draft)
                raise ValueError("ASR returned no usable text")
            cancel.check()
            draft = replace(draft, state="pending_confirmation", transcript=result.text)
            self._save(draft)
            return draft

    def confirm(self, capture_id: str, text: str) -> SpeechDraft:
        """用户可编辑后确认；同一确认幂等，已确认草稿不能暗中换文本。"""
        draft = self._drafts[capture_id]
        if not text.strip() or len(text) > 8000:
            raise ValueError("confirmed speech text must be nonempty and bounded")
        if draft.state == "confirmed" and draft.confirmed_text == text:
            return draft
        if draft.state != "pending_confirmation":
            raise ValueError("speech draft is not awaiting confirmation")
        draft = replace(draft, state="confirmed", confirmed_text=text)
        self._save(draft)
        return draft

    def discard(self, capture_id: str) -> SpeechDraft:
        """取消待确认草稿，保留录音及已发生消费证据，不向会话提交。"""
        draft = self._drafts[capture_id]
        if draft.state != "pending_confirmation":
            raise ValueError("speech draft cannot be discarded in its current state")
        draft = replace(draft, state="discarded")
        self._save(draft)
        return draft

    def get(self, capture_id: str) -> SpeechDraft:
        """只读当前宿主草稿；本阶段不自动恢复或重新转写旧文件。"""
        return self._drafts[capture_id]

    def _save(self, draft: SpeechDraft) -> None:
        """原子替换草稿快照；只有落盘完成才向调用方返回可确认状态。"""
        target = self.root / (draft.capture_id + ".json")
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(asdict(draft), ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self._drafts[draft.capture_id] = draft

    @staticmethod
    def _validate(recording: Recording) -> None:
        """只接收8～96kHz单声道16位PCM，核对实际帧数；ASR适配器负责重采样。"""
        if not 100 <= recording.duration_ms <= 60000 or not 0 < len(recording.wav) <= 16777216:
            raise ValueError("recording duration or size exceeds limit")
        with wave.open(io.BytesIO(recording.wav), "rb") as audio:
            frames = audio.getnframes()
            if (audio.getnchannels(), audio.getsampwidth()) != (
                1,
                2,
            ) or not 8000 <= audio.getframerate() <= 96000:
                raise ValueError("recording PCM format mismatch")
            if (
                frames * 1000 // audio.getframerate() != recording.duration_ms
                or len(audio.readframes(frames)) != frames * 2
            ):
                raise ValueError("recording duration or frame bytes mismatch")
