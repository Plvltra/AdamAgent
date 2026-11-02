"""按真实时间轴组织帧语义；只断言样本中可见的顺序，不猜未观察区间。"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef
from adam.video.models import FrameRef, VideoFrames


@dataclass(frozen=True)
class FrameMeaning:
    """一次帧分析的标签、原帧证据与不确定性；source说明真实分析来源。"""

    frame: MediaRef
    label: str
    uncertain: bool
    source: str


@dataclass(frozen=True)
class VideoEvent:
    """起止是同标签证据帧的观测时间，不是精确发生/结束时刻。"""

    label: str
    start_seconds: float
    end_seconds: float
    frames: tuple[MediaRef, ...]
    analysis_sources: tuple[str, ...]
    uncertain: bool


@dataclass(frozen=True)
class SequenceReport:
    """仅描述样本标签顺序，不证明标签正确；顺序未知时不得输出确定先后。"""

    video: MediaRef
    events: tuple[VideoEvent, ...]
    order_known: bool
    issues: tuple[str, ...]
    expected_samples: int
    analyzed_samples: int
    coverage: str = "sampled_frames_only"


class TemporalAnalyzer:
    """通过窄帧分析回调工作；不直接导入视觉模型SDK或启动新Agent循环。"""

    def __init__(
        self, analyze_frame: Callable[[FrameRef, CancellationToken], FrameMeaning]
    ) -> None:
        self.analyze_frame = analyze_frame

    def analyze(
        self,
        video: VideoFrames,
        *,
        cancel: CancellationToken,
        frames: Sequence[FrameRef] | None = None,
    ) -> SequenceReport:
        """先核对来源/顺序，再分析样本；缺帧降低确定性，失败或取消不自动重问。

        frames为空参数表示用全部原样本；显式子集用于丢帧观察。外来帧、重复帧、
        非递增PTS或重排直接返回顺序未知，不静默排序掩盖采集错误。
        """
        cancel.check()
        expected = video.frames
        selected = tuple(expected if frames is None else frames)
        lookup = {frame.media.media_id: (index, frame) for index, frame in enumerate(expected)}
        if (
            not expected
            or len(expected) > 128
            or any(
                not isfinite(f.timestamp_seconds)
                or f.time_base_den <= 0
                or f.time_base_num <= 0
                or f.timestamp_seconds != float(f.pts * f.time_base_num / f.time_base_den)
                for f in expected
            )
            or len(lookup) != len(expected)
            or any(f.media.source != video.video.source for f in expected)
            or any(
                a.timestamp_seconds >= b.timestamp_seconds
                for a, b in zip(expected, expected[1:], strict=False)
            )
        ):
            return SequenceReport(
                video.video, (), False, ("invalid_original_timeline",), len(expected), 0
            )
        indices = []
        for frame in selected:
            found = lookup.get(frame.media.media_id)
            if found is None or found[1] != frame:
                return SequenceReport(
                    video.video, (), False, ("foreign_or_changed_frame",), len(expected), 0
                )
            indices.append(found[0])
        if any(a >= b for a, b in zip(indices, indices[1:], strict=False)):
            return SequenceReport(
                video.video, (), False, ("out_of_order_or_duplicate",), len(expected), 0
            )
        issues = []
        if len(selected) != len(expected):
            issues.append("missing_samples")
        if not selected:
            issues.append("no_observations")
        events: list[VideoEvent] = []
        previous_index = -2
        for index, frame in zip(indices, selected, strict=True):
            cancel.check()
            meaning = self.analyze_frame(frame, cancel)
            cancel.check()
            if meaning.frame != frame.media:
                raise ValueError("frame analysis evidence mismatch")
            if not meaning.label.strip() or len(meaning.label) > 1000 or not meaning.source:
                raise ValueError("invalid frame analysis")
            uncertain = meaning.uncertain or bool(issues)
            if meaning.uncertain and "uncertain_frame_analysis" not in issues:
                issues.append("uncertain_frame_analysis")
            if events and index == previous_index + 1 and events[-1].label == meaning.label:
                old = events.pop()
                events.append(
                    VideoEvent(
                        old.label,
                        old.start_seconds,
                        frame.timestamp_seconds,
                        old.frames + (frame.media,),
                        old.analysis_sources + (meaning.source,),
                        old.uncertain or uncertain,
                    )
                )
            else:
                events.append(
                    VideoEvent(
                        meaning.label,
                        frame.timestamp_seconds,
                        frame.timestamp_seconds,
                        (frame.media,),
                        (meaning.source,),
                        uncertain,
                    )
                )
            previous_index = index
        return SequenceReport(
            video.video,
            tuple(events),
            not issues and bool(events),
            tuple(issues),
            len(expected),
            len(selected),
        )
