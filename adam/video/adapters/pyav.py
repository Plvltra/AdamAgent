"""PyAV适配器；仅解码完整本地MP4/MOV或Matroska/WebM字节。"""

from __future__ import annotations

import io
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from av.container import InputContainer

from adam.contracts.cancellation import CancellationToken
from adam.video.models import DecodedFrame, DecodedVideo, VideoError, VideoLimits


class PyAVDecoder:
    """延迟导入可选后端；不接受URL，不自动下载或抽取音频。"""

    def __init__(self, limits: VideoLimits | None = None) -> None:
        self.limits = limits or VideoLimits()

    def decode(self, data: bytes, *, cancel: CancellationToken) -> DecodedVideo:
        """按真实PTS顺序采样，扫描整片以发现尾部超限；阶段边界协作取消。

        不以平均FPS补时间，不把缺帧时间插值为新图。原生单帧解码不能硬中断；
        输入/流头/逐帧都限额，仍不承诺抵御解码器内部的恶意资源分配。
        """
        cancel.check()
        if not data or len(data) > self.limits.max_bytes:
            raise VideoError("size", "视频字节为空或超过上限。")
        try:
            import av
            from av.error import FFmpegError
        except ImportError as error:
            raise VideoError("unavailable", "未安装视频解码后端。") from error
        try:
            with av.open(
                io.BytesIO(data),
                mode="r",
                options={"format_whitelist": "mov,matroska,webm", "protocol_whitelist": ""},
            ) as container:
                return self._decode(container, cancel)
        except FFmpegError as error:
            raise VideoError("corrupt", "视频无法解码或格式不支持。") from error
        raise VideoError("incomplete", "视频解码未产生结果。")

    def _decode(self, container: InputContainer, cancel: CancellationToken) -> DecodedVideo:
        """限制首条视频流并保留所有采样帧，任一失败不交付部分成功结果。"""
        if not container.streams.video:
            raise VideoError("no_video", "媒体没有视频流。")
        stream = container.streams.video[0]
        self._check_size(stream.codec_context.width, stream.codec_context.height)
        stream.codec_context.thread_count = 1
        duration = float(container.duration / 1000000) if container.duration is not None else None
        if duration is not None and (duration < 0 or duration > self.limits.max_seconds):
            raise VideoError("duration", "视频时长超过上限或无效。")
        frames: list[DecodedFrame] = []
        first: Fraction | None = None
        previous: Fraction | None = None
        sampled: Fraction | None = None
        count = total = 0
        for frame in container.decode(stream):
            cancel.check()
            count += 1
            if count > self.limits.max_decoded_frames:
                raise VideoError("decode_limit", "视频解码帧数超过上限。")
            self._check_size(frame.width, frame.height)
            if frame.pts is None or frame.time_base is None:
                raise VideoError("timestamp", "视频帧缺少真实时间戳。")
            base = Fraction(frame.time_base)
            stamp = frame.pts * base
            if base <= 0 or (previous is not None and stamp <= previous):
                raise VideoError("timestamp", "视频时间戳不严格递增。")
            first = stamp if first is None else first
            if stamp - first > self.limits.max_seconds:
                raise VideoError("duration", "实际帧时间轴超过时长上限。")
            previous = stamp
            if sampled is not None and float(stamp - sampled) < self.limits.sample_seconds:
                continue
            if len(frames) >= self.limits.max_samples:
                raise VideoError("sample_limit", "采样数超过上限，请增大采样间隔。")
            output = io.BytesIO()
            frame.to_image().save(output, format="PNG")
            png = output.getvalue()
            total += len(png)
            if total > self.limits.max_sample_bytes:
                raise VideoError("sample_bytes", "视频采样字节超过上限。")
            frames.append(
                DecodedFrame(
                    frame.pts,
                    base.numerator,
                    base.denominator,
                    float(stamp),
                    frame.width,
                    frame.height,
                    png,
                )
            )
            sampled = stamp
        cancel.check()
        if not frames:
            raise VideoError("empty", "视频没有可解码帧。")
        mime = "video/mp4" if "mov" in container.format.name else "video/x-matroska"
        return DecodedVideo(mime, duration, count, tuple(frames))

    def _check_size(self, width: int, height: int) -> None:
        """在转换PNG之前拒绝超大或缺失尺寸，不静默缩小证据。"""
        if width <= 0 or height <= 0 or width * height > self.limits.max_pixels:
            raise VideoError("resolution", "视频分辨率无效或超过像素上限。")
