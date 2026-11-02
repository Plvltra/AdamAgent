"""按键开始/结束的PortAudio录音；时长、内存、静音与溢出都有明确失败。"""

import importlib
import io
import math
import struct
import wave
from threading import RLock, Timer
from typing import Any

from adam.audio.models import Recording


class Microphone:
    """单设备单录音；超时自动停止并关闭设备，finish不会返回截短伪成功。"""

    def __init__(self, device: int | str | None = None, *, max_seconds: float = 30) -> None:
        if not 0.1 <= max_seconds <= 60:
            raise ValueError("recording limit must be 0.1 to 60 seconds")
        self.device, self.max_seconds = device, max_seconds
        self._lock = RLock()
        self._stream: Any = None
        self._timer: Timer | None = None
        self._chunks: list[bytes] = []
        self._bytes = 0
        self._error: str | None = None
        self._name = ""
        self._rate = 16000
        self._started = False

    def start(self) -> None:
        """只有显式按键才打开麦克风；设备缺失不创建空录音或隐式换设备。"""
        with self._lock:
            if self._started:
                raise ValueError("recording already started")
            sd = importlib.import_module("sounddevice")
            info = sd.query_devices(self.device, "input")
            if info["max_input_channels"] < 1:
                raise ValueError("input device is unavailable")
            self._name = str(info["name"])
            self._rate = int(info["default_samplerate"])
            if not 8000 <= self._rate <= 96000:
                raise ValueError("input sample rate is unsupported")
            self._chunks, self._bytes, self._error = [], 0, None

            def receive(data: bytes, frames: int, timing: object, status: object) -> None:
                # 回调只复制有界PCM；磁盘、转写和关闭设备都在回调之外执行。
                raw = bytes(data)
                if status or self._bytes + len(raw) > int(self.max_seconds * self._rate * 2):
                    self._error = "recording overflow or duration exceeded"
                    raise sd.CallbackAbort()
                self._chunks.append(raw)
                self._bytes += len(raw)

            stream = sd.RawInputStream(
                device=self.device,
                samplerate=self._rate,
                channels=1,
                dtype="int16",
                callback=receive,
            )
            try:
                stream.start()
            except BaseException:
                stream.close()
                raise
            self._stream, self._started = stream, True
            self._timer = Timer(self.max_seconds, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def finish(self) -> Recording:
        """先关闭设备，再核对实际PCM；不足100ms、静音或溢出明确失败。"""
        with self._lock:
            if not self._started:
                raise ValueError("no recording is active")
            self._close()
            self._started = False
            raw = b"".join(self._chunks)
            self._chunks = []
            if self._error:
                raise ValueError(self._error)
            if len(raw) < self._rate // 5 or len(raw) % 2:
                raise ValueError("recording is empty or too short")
            samples = struct.iter_unpack("<h", raw)
            rms = math.sqrt(sum(value[0] ** 2 for value in samples) / (len(raw) // 2))
            if rms < 40:
                raise ValueError("recording is silent")
            output = io.BytesIO()
            with wave.open(output, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(self._rate)
                audio.writeframes(raw)
            return Recording(output.getvalue(), (len(raw) // 2) * 1000 // self._rate, self._name)

    def cancel(self) -> None:
        """丢弃未确认录音并释放设备；不调用ASR，不提交空文本。"""
        with self._lock:
            self._close()
            self._started = False
            self._chunks = []

    def _expire(self) -> None:
        """墙钟上限覆盖回调不再供帧的故障；结束按键随后得到超长错误。"""
        with self._lock:
            if self._started:
                self._error = "recording duration exceeded"
                self._close()

    def _close(self) -> None:
        """中止本实例流并关闭句柄；一项清理失败仍尝试另一项。"""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()
