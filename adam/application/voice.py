"""确认语音复用原应用send；固定身份先留证，失败或重启不重新提交模型。"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from adam.application.conversation import Conversation
from adam.application.facade import AgentApplication
from adam.contracts.errors import StorageError
from adam.contracts.media import MediaSource
from adam.contracts.speech import ConfirmedSpeech
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore


class VoiceConversation:
    """单宿主串行语音入口；复用原模式/权限/批准，原录音不作为模型音频输入发送。"""

    def __init__(
        self,
        app: AgentApplication,
        conversation: Conversation,
        store: SessionStore,
        media: SessionMedia,
        confirmed: Callable[[str], ConfirmedSpeech],
    ) -> None:
        self.app, self.conversation, self.store, self.media, self.confirmed = (
            app,
            conversation,
            store,
            media,
            confirmed,
        )
        self.root = store.root / "_media" / "voice_submissions"
        self._lock = RLock()

    def send(self, capture_id: str) -> dict[str, Any]:
        """仅第一次提交已确认文字；同ID重试只查询，原状态未知时也不补发。"""
        with self._lock:
            speech = self.confirmed(capture_id)
            path = self._path(capture_id)
            if path.exists():
                prior = self.query(capture_id)
                if prior["text"] != speech.text or prior["audio_version"] != speech.version:
                    raise StorageError("语音身份已绑定其他文字或录音，未重新发送。")
                return prior
            session = self.app.session or self.app.new(speech.text[:40])
            receipt = {
                "capture_id": capture_id,
                "session_id": session.session_id,
                "text": speech.text,
                "audio_version": speech.version,
                "device": speech.device,
                "state": "submitting",
                "turn_id": None,
                "media": None,
                "reply": None,
            }
            self.root.mkdir(parents=True, exist_ok=True)
            # 排他创建是跨宿主的去重边界；写到一半的坏文件也不能被当作未发送后重试。
            with path.open("x", encoding="utf-8") as output:
                json.dump(receipt, output, ensure_ascii=False)
            previous = self.conversation.on_turn_started

            def started(turn_id: str) -> None:
                """沿原回调登记输入后，将同一次真实轮次与音频来源绑定，之后才请求模型。"""
                if previous:
                    previous(turn_id)
                if self.store.active_session_id != receipt["session_id"]:
                    raise StorageError("语音提交期间会话发生变化。")
                receipt["turn_id"] = turn_id
                self._save(path, receipt)
                source = MediaSource(receipt["session_id"], turn_id, "input")
                receipt["media"] = asdict(self.media.publish(source, speech.audio, "audio/wav"))
                self._save(path, receipt)

            self.conversation.on_turn_started = started
            try:
                reply = self.app.send(speech.text)
                receipt.update(state="succeeded", reply=reply)
                self._save(path, receipt)
                return receipt
            except BaseException as error:
                receipt["state"] = "unknown"
                try:
                    self._save(path, receipt)
                except Exception:
                    error.add_note("语音提交收尾保存失败；原身份已占用，不能重新发送。")
                raise
            finally:
                self.conversation.on_turn_started = previous

    def query(self, capture_id: str) -> dict[str, Any]:
        """只查当前会话；成功历史可修复收尾丢失，其余不确定状态保持unknown。"""
        with self._lock:
            path = self._path(capture_id)
            if path.stat().st_size > 262144:
                raise StorageError("语音提交记录超过上限。")
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                value["capture_id"] != capture_id
                or value["session_id"] != self.store.active_session_id
            ):
                raise StorageError("语音记录不属于当前会话。")
            if value["state"] not in {"submitting", "unknown", "succeeded"}:
                raise StorageError("语音提交状态损坏。")
            turns = self.store.load_turns(value["session_id"])
            turn = next((item for item in turns if item.turn_id == value["turn_id"]), None)
            if turn is not None:
                first, last = turn.messages[0], turn.messages[-1]
                if first.get("content") != value["text"] or last.get("role") != "assistant":
                    raise StorageError("语音提交记录与原成功轮次不符。")
                value.update(state="succeeded", reply=last["content"])
            else:
                value.update(state="unknown", reply=None)
            if value["media"] is not None:
                ref = value["media"]
                self.media.read(ref["media_id"], expected_version=ref["version"])
            self._save(path, value)
            return value

    def _path(self, capture_id: str) -> Path:
        """只允许固定capture_id，模型文本不能变成任意路径。"""
        if re.fullmatch(r"[0-9a-f]{32}", capture_id) is None:
            raise ValueError("invalid speech capture identity")
        return self.root / (capture_id + ".json")

    @staticmethod
    def _save(path: Path, value: dict[str, Any]) -> None:
        """原子保存同一提交身份；写失败不清除首次占用，不释放重新派发机会。"""
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
