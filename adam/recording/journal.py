"""独立录制文件的有界顺序写入与只读加载；不修复或修改原会话。"""

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from adam.contracts.media import MediaRef
from adam.recording.format import (
    RecordingError,
    RecordingLimits,
    TraceState,
    decode_event,
    encode_event,
)
from adam.recording.media import MediaArchive

DEFAULT_LIMITS = RecordingLimits()


@dataclass(frozen=True)
class Recording:
    """已验证的录制前缀；complete 只表示录制有明确结尾，不代表业务成功。"""

    events: tuple[dict[str, Any], ...]
    complete: bool
    torn_tail: bool
    media: tuple[MediaRef, ...] = ()

    @property
    def seed(self) -> dict[str, Any]:
        """取得开始前快照；返回来自本次文件读取的数据，不访问原项目。"""
        return self.events[0]["data"]["seed"]


class RecordingWriter:
    """单线程、单文件、仅新建的录制器；关闭不会自动伪造结束记录。"""

    def __init__(
        self, path: Path, seed: dict[str, Any], limits: RecordingLimits = DEFAULT_LIMITS
    ) -> None:
        self.path = path.absolute()
        self.limits = limits
        self._state = TraceState()
        self._owner = threading.get_ident()
        self._broken = False
        self._size = 0
        header = {"seq": 0, "type": "recording_started", "data": {"version": 1, "seed": seed}}
        try:
            raw = encode_event(header)
            self._validate_size(raw)
            self._state.accept(decode_event(raw))
            self.seed_event_count = len(seed["session_events"])
            # xb 在检查与创建之间保持排他；不覆盖已有录制或原会话。
            self._file = path.open("xb")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
            raise RecordingError("无法创建录制：路径已存在、不可写或起点无效。") from error
        try:
            self._write(raw)
        except BaseException:
            self._file.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def next_sequence(self) -> int:
        """下一条事件的序号，同时作为新调用的录制内身份。"""
        return self._state.count

    def append(self, kind: str, data: dict[str, Any]) -> None:
        """先验证顺序及容量，再写入并 fsync；任何失败后此写入器不再接受事件。"""
        if self._file.closed or self._broken or threading.get_ident() != self._owner:
            raise RecordingError("录制已关闭、发生故障或不属于当前线程。")
        try:
            raw = encode_event({"seq": self._state.count, "type": kind, "data": data})
            self._validate_size(raw)
            self._state.accept(decode_event(raw))
            self._write(raw)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
            self._broken = True
            raise RecordingError("录制保存失败；已停止，不自动重试已发生的动作。") from error

    def close(self) -> None:
        """关闭句柄；缺少显式结束事件的文件保持不完整。"""
        self._file.close()

    def _validate_size(self, raw: bytes) -> None:
        if (
            len(raw) > self.limits.max_event_bytes
            or self._size + len(raw) > self.limits.max_bytes
            or self._state.count >= self.limits.max_events
        ):
            raise ValueError("recording limit exceeded")

    def _write(self, raw: bytes) -> None:
        """落盘成功后才推进字节计数；可捕获写入失败尽力截回已确认前缀。"""
        offset = self._size
        try:
            if self._file.write(raw) != len(raw):
                raise OSError("short recording write")
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as error:
            self._broken = True
            try:
                self._file.seek(offset)
                self._file.truncate()
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError:
                pass
            raise RecordingError("录制写入失败；文件可能不完整，未重做原动作。") from error
        self._size += len(raw)


def read_recording(path: Path, limits: RecordingLimits = DEFAULT_LIMITS) -> Recording:
    """读取并验证有限完整前缀；只忽略最后一个未完成的物理行，文件本身保持不变。

    媒体输入须有完整旁路副本且通过摘要与来源验证；不访问原会话或创建锁文件。
    中间损坏、错误关联、未知版本和上限超出均拒绝。没有结尾或存在残缺尾行时，
    complete=False；调用方只能诊断已知前缀，不能宣称整轮已重放成功。
    """
    try:
        with path.open("rb") as file:
            raw = file.read(limits.max_bytes + 1)
        return parse_recording(raw, limits)
    except OSError as error:
        raise RecordingError("录制无法读取：路径不存在或无读取权限。") from error


def parse_recording(raw: bytes, limits: RecordingLimits = DEFAULT_LIMITS) -> Recording:
    """校验同一份有界字节快照，供读取与分享导出共用；残缺尾部规则不变。

    导出来源摘要与内容必须来自同一份 raw，不能两次读取活动文件后错绑版本。
    本函数不修复原文件，也不将缺失结尾的录制升级为完整。
    """
    try:
        if len(raw) > limits.max_bytes:
            raise ValueError("oversized recording")
        lines = raw.split(b"\n")
        tail = lines.pop()
        torn = bool(tail)
        state = TraceState()
        events: list[dict[str, Any]] = []
        if len(lines) > limits.max_events or len(tail) > limits.max_event_bytes:
            raise ValueError("too many recording events")
        for line in lines:
            if len(line) + 1 > limits.max_event_bytes:
                raise ValueError("oversized recording event")
            event = decode_event(line)
            state.accept(event)
            events.append(event)
        if not events or (torn and state.closed):
            raise ValueError("missing header or data after recording finish")
        # 完整 JSON 仅少换行与真正残缺尾行都保持不完整；不推断落盘是否成功。
        references = tuple(state.media.values())
        MediaArchive(path).verify(references)
        return Recording(tuple(events), state.closed and not torn, torn, references)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        raise RecordingError("录制无法读取：文件损坏、版本或关联无效，或超过读取上限。") from error
