"""按键语音宿主；会话和模型只在主线程使用，输入线程仅投递B27补充。"""

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, RLock, Thread
from typing import Any, Protocol

from adam.application.voice import VoiceConversation
from adam.audio.input import SpeechInput
from adam.audio.models import Recording
from adam.audio.playback import SpeechPlayback
from adam.contracts.cancellation import CancellationToken


class Recorder(Protocol):
    """录音设备端口；finish和cancel必须释放本次设备。"""

    def start(self) -> None: ...
    def finish(self) -> Recording: ...
    def cancel(self) -> None: ...


class VoiceLoop:
    """唯一按键读线程；原应用send与来源日志由调用run的主线程负责。"""

    def __init__(
        self,
        voice: VoiceConversation,
        speech: SpeechInput,
        recorder: Recorder,
        playback: SpeechPlayback,
        cancel: CancellationToken,
        read: Callable[[str], str],
        write: Callable[[str], None],
    ) -> None:
        self.voice, self.speech, self.recorder, self.playback = voice, speech, recorder, playback
        self.cancel, self.read, self.write = cancel, read, write
        self._lock = RLock()
        self._closed = Event()
        self._audio_cancel = CancellationToken()
        self._pending: Queue[str] = Queue(maxsize=1)
        self._active: tuple[str, str] | None = None
        self._recording = False
        self._thread: Thread | None = None

    def run(self) -> None:
        """Enter开始/结束，编辑确认；/quit退出。录音或队列失败保留草稿，不自动重发。"""
        previous = self.voice.conversation.on_turn_started

        def started(turn_id: str) -> None:
            """沿原回调先建立B27队列，再向输入线程发布固定轮次身份。"""
            if previous:
                previous(turn_id)
            session = self.voice.app.session
            assert session is not None
            with self._lock:
                self._active = (session.session_id, turn_id)

        self.voice.conversation.on_turn_started = started
        self._thread = Thread(target=self._input, name="adam-voice-input", daemon=True)
        self._thread.start()
        try:
            while not self._closed.is_set():
                try:
                    capture_id = self._pending.get(timeout=0.1)
                except Empty:
                    continue
                if self._closed.is_set():
                    break
                try:
                    result = self.voice.send(capture_id)
                    self.write(str(result["reply"]))
                    with self._lock:
                        self._active = None
                        if (
                            not self._recording
                            and not self._closed.is_set()
                            and self._pending.empty()
                        ):
                            lease = self.playback.begin(result["session_id"], result["turn_id"])
                            self.playback.start(lease, result["reply"])
                except (Exception, KeyboardInterrupt) as error:
                    self.write("本轮未完成，未自动重发：" + type(error).__name__)
                finally:
                    with self._lock:
                        self._active = None
        finally:
            self._closed.set()
            self._audio_cancel.cancel()
            self.recorder.cancel()
            self.playback.close()
            self._thread.join(timeout=6)
            self.voice.conversation.on_turn_started = previous

    def _input(self) -> None:
        """唯一stdin读者；录音前先停播，补充只用原线程安全接口，不读取SessionStore。"""
        try:
            while not self._closed.is_set():
                command = self.read("Enter开始讲话；/quit退出：")
                if command == "/quit":
                    break
                if self._closed.is_set():
                    break
                with self._lock:
                    self._recording = True
                    self.playback.close()
                try:
                    self.recorder.start()
                    stop = self.read("录音中，Enter结束；/cancel放弃：")
                    if stop == "/cancel":
                        continue
                    audio = self.recorder.finish()
                    draft = self.speech.transcribe(audio, cancel=self._audio_cancel)
                    self.write("转写：" + draft.transcript)
                    edited = self.read("Enter确认，或输入修正文；/cancel放弃：")
                    if edited == "/cancel":
                        self.speech.discard(draft.capture_id)
                        continue
                    draft = self.speech.confirm(draft.capture_id, edited or draft.transcript)
                    with self._lock:
                        if self._closed.is_set():
                            break
                        if self._active:
                            self._supplement(
                                draft.capture_id, str(draft.confirmed_text), draft.audio_version
                            )
                        else:
                            try:
                                self._pending.put_nowait(draft.capture_id)
                                self.write("已排入下一轮：" + draft.capture_id)
                            except Full:
                                self.write("待处理语音已满；草稿保留，未发送：" + draft.capture_id)
                except (Exception, KeyboardInterrupt) as error:
                    self.write("语音输入未完成，未自动重发：" + type(error).__name__)
                finally:
                    self.recorder.cancel()
                    with self._lock:
                        self._recording = False
        except (EOFError, OSError):
            pass
        finally:
            self._closed.set()
            self._audio_cancel.cancel()
            self.cancel.cancel()

    def _supplement(self, capture_id: str, text: str, version: str) -> None:
        """独立记录绑定原草稿与固定活轮次；B27接受失败不改走下一轮，避免重复提交。"""
        assert self._active is not None
        session_id, turn_id = self._active
        root = self.voice.root.parent / "voice_supplements"
        root.mkdir(parents=True, exist_ok=True)
        path: Path = root / (capture_id + ".json")
        value: dict[str, Any] = dict(
            capture_id=capture_id,
            session_id=session_id,
            turn_id=turn_id,
            text=text,
            audio_version=version,
            state="submitting",
        )
        with path.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
        try:
            receipt = self.voice.app.submit_input(text, capture_id)
            value.update(state="accepted", receipt=asdict(receipt))
        except BaseException:
            value["state"] = "unknown"
            raise
        finally:
            temporary = path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        self.write("补充已接受：" + capture_id)
