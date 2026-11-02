"""语音草稿与计量端口；转写内容是待确认的普通文本，不赋予额外权限。"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionSnapshot, Quantities


@dataclass(frozen=True)
class Recording:
    """实际麦克风PCM编码后的完整WAV；时长来自实际帧数，device仅为来源说明。"""

    wav: bytes
    duration_ms: int
    device: str


@dataclass(frozen=True)
class Transcription:
    """后端原始文本与实际音频单位；空结果不能作为空用户请求提交。"""

    text: str
    output_tokens: int | None


class Transcriber(Protocol):
    """只转写一次，取消须停止本次推理，不自动重试。"""

    def transcribe(self, audio: bytes, *, cancel: CancellationToken) -> Transcription: ...


class AudioBudget(Protocol):
    """注入原C00 UnitBudget，转写不自行创建另一套额度。"""

    def reserve(self, estimate: Quantities) -> AbstractContextManager[None]: ...

    def update(self, owner: str, usage: ConsumptionSnapshot) -> None: ...
