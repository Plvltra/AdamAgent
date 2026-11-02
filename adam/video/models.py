"""有界视频帧与后端端口；时间来自媒体 PTS，不按平均帧率猜测。"""

from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef


class VideoError(Exception):
    """可诊断的解码失败；固定kind不泄露解码器原始内容。"""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class VideoLimits:
    """限制输入、时间轴、像素、解码数及采样字节；不是原生解码内存沙箱。"""

    max_bytes: int = 33554432
    max_seconds: float = 30.0
    max_pixels: int = 2073600
    max_decoded_frames: int = 3600
    max_samples: int = 32
    sample_seconds: float = 1.0
    max_sample_bytes: int = 33554432

    def __post_init__(self) -> None:
        if not 0 < self.max_seconds <= 120 or not 0.01 <= self.sample_seconds <= 120:
            raise ValueError("invalid video time limits")
        for name, maximum in (
            ("max_bytes", 134217728),
            ("max_pixels", 8294400),
            ("max_decoded_frames", 14400),
            ("max_samples", 128),
            ("max_sample_bytes", 134217728),
        ):
            if type(getattr(self, name)) is not int or not 0 < getattr(self, name) <= maximum:
                raise ValueError("invalid video resource limit")


@dataclass(frozen=True)
class DecodedFrame:
    """原视频PTS和time_base保留精确依据，PNG是该实际解码帧。"""

    pts: int
    time_base_num: int
    time_base_den: int
    timestamp_seconds: float
    width: int
    height: int
    png: bytes


@dataclass(frozen=True)
class DecodedVideo:
    """完整有界遍历后的样本；duration为空表示容器没有报告可信时长。"""

    mime_type: str
    duration_seconds: float | None
    decoded_frames: int
    frames: tuple[DecodedFrame, ...]


@dataclass(frozen=True)
class FrameRef:
    """帧在原媒体时间轴的位置及已发布引用；不是模型分析结论。"""

    pts: int
    time_base_num: int
    time_base_den: int
    timestamp_seconds: float
    width: int
    height: int
    media: MediaRef


@dataclass(frozen=True)
class VideoFrames:
    """原视频与采样帧共同绑定真实输入/工具来源。"""

    video: MediaRef
    duration_seconds: float | None
    decoded_frames: int
    frames: tuple[FrameRef, ...]


class VideoDecoder(Protocol):
    def decode(self, data: bytes, *, cancel: CancellationToken) -> DecodedVideo:
        """完整遍历有界短片后返回帧，损坏/超限/时间轴无效时失败。"""
        ...
