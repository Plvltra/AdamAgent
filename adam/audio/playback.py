"""轮次播报租约；旧回复不得启动播放，打断必须等待设备释放后才投递输入。"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock, Thread
from typing import TypeVar

from adam.audio.output import SpeechOutput
from adam.contracts.cancellation import CancellationToken

Result = TypeVar("Result")


@dataclass(frozen=True)
class PlaybackTurn:
    """宿主绑定的会话/轮次与本次播放身份；旧身份不能作用于下一次播报。"""

    session_id: str
    turn_id: str
    playback_id: str


class SpeechPlayback:
    """单宿主串行控制一条播放线程；后台仅朗读，不访问模型或会话JSONL。"""

    def __init__(self, output: SpeechOutput, *, stop_timeout: float = 6) -> None:
        if not 0 < stop_timeout <= 10:
            raise ValueError("invalid playback stop timeout")
        self.output, self.stop_timeout = output, stop_timeout
        self._lock = RLock()
        self._lease: PlaybackTurn | None = None
        self._thread: Thread | None = None
        self._cancel = CancellationToken()
        self._started = False
        self.error: BaseException | None = None

    def begin(self, session_id: str, turn_id: str) -> PlaybackTurn:
        """先结束旧播报再换身份；宿主应在原轮次开始回调调用，不复用旧回复。"""
        with self._lock:
            self._lease = None
            self._stop()
            self._lease = PlaybackTurn(session_id, turn_id, secrets.token_hex(16))
            self._started = False
            self.error = None
            return self._lease

    def start(self, lease: PlaybackTurn, text: str) -> None:
        """只启动当前身份一次；合成/播放错误留在error和原独立回执，不重发模型。"""
        with self._lock:
            self._require(lease)
            if self._started:
                raise ValueError("playback already started")
            self._started = True
            self._cancel = CancellationToken()

            def run() -> None:
                """工作线程只执行本身份；取消与失败均留痕，禁止自动补播。"""
                try:
                    self.output.speak(
                        lease.session_id,
                        lease.turn_id,
                        text,
                        cancel=self._cancel,
                        playback_id=lease.playback_id,
                    )
                except BaseException as error:
                    self.error = error

            self._thread = Thread(target=run, name="adam-speech-playback")
            self._thread.start()

    def interrupt(self, lease: PlaybackTurn, submit: Callable[[], Result]) -> Result:
        """使旧身份失效并等待停止，再调用宿主投递；停止失败绝不提交新输入。

        活轮次宿主传入原app.submit_input回调，消费位置仍由B27安全点决定；
        空闲轮次须显式选择下一轮入口，不根据投递异常猜测并补发。
        """
        with self._lock:
            self._require(lease)
            self._lease = None
            self._stop()
            return submit()

    def close(self) -> None:
        """失效当前身份并等待本线程清理；超时明确失败，不宣称设备已释放。"""
        with self._lock:
            self._lease = None
            self._stop()

    def _require(self, lease: PlaybackTurn) -> None:
        """完整匹配会话、轮次和随机播放身份，拒绝迟到回复或重复打断。"""
        if lease != self._lease:
            raise ValueError("stale playback identity")

    def _stop(self) -> None:
        """先发取消，再等待后端finally释放资源；线程仍活着时保留句柄。"""
        self._cancel.cancel()
        if self._thread is not None:
            self._thread.join(self.stop_timeout)
            if self._thread.is_alive():
                raise TimeoutError("playback did not release device; input was not submitted")
            self._thread = None
