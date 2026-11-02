"""按原PTS播放已发布帧；单个待处理槽丢弃积压，不把采样缺口当连续证据。"""

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from threading import Condition, Event, Thread
from time import monotonic

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef
from adam.video.models import FrameRef, VideoError, VideoFrames
from adam.video.temporal import FrameMeaning, SequenceReport, TemporalAnalyzer


@dataclass(frozen=True)
class DroppedFrame:
    """原帧被替换或分析前已过期；age_seconds相对计划播放时刻。"""

    frame: MediaRef
    reason: str
    age_seconds: float


@dataclass(frozen=True)
class LiveObservation:
    """分析结果与真实墙钟延迟；latency_seconds含排队和分析耗时。"""

    frame: FrameRef
    meaning: FrameMeaning
    queue_seconds: float
    analysis_seconds: float
    latency_seconds: float


@dataclass(frozen=True)
class PlaybackReport:
    """只证明测试视频实时播放；零计时间隔的速率未知，费用沿用原请求日志。"""

    sequence: SequenceReport
    observations: tuple[LiveObservation, ...]
    dropped: tuple[DroppedFrame, ...]
    elapsed_seconds: float
    offered_rate_hz: float | None
    analyzed_rate_hz: float | None
    source_kind: str = "realtime_video_playback"


class RealtimePlayback:
    """后台按PTS供帧，调用线程串行分析；待处理槽始终最多一帧。

    消费者跟不上时替换旧待处理帧，并在分析前检查年龄。分析回调必须遵循
    取消令牌，不能硬中断原生或模型调用；失败不重试，已发生的请求仍由原日志留证。
    """

    def __init__(
        self,
        analyze_frame: Callable[[FrameRef, CancellationToken], FrameMeaning],
        *,
        max_age_seconds: float = 1.0,
    ) -> None:
        if not isfinite(max_age_seconds) or not 0 < max_age_seconds <= 120:
            raise ValueError("invalid maximum observation age")
        self.analyze_frame = analyze_frame
        self.max_age_seconds = max_age_seconds

    def observe(self, video: VideoFrames, *, cancel: CancellationToken) -> PlaybackReport:
        """消费完整有界播放后返回实际分析子集；取消/失败时停止并回收供帧线程。

        使用C06.2原时间轴和来源校验，不读取媒体或改变PTS；最长播放120秒。
        仅完成的分析进入事件整理，缺帧按原时序规则标记不确定，整理不再次请求模型。
        """
        validation = TemporalAnalyzer(self.analyze_frame).analyze(video, frames=(), cancel=cancel)
        if "invalid_original_timeline" in validation.issues:
            raise VideoError("timeline", "Cannot play an invalid frame timeline.")
        span = video.frames[-1].timestamp_seconds - video.frames[0].timestamp_seconds
        if span > 120:
            raise VideoError("duration", "Playback exceeds the bounded experiment duration.")
        started = monotonic()
        condition, stop = Condition(), Event()
        pending: list[tuple[FrameRef, float]] = []
        dropped: list[DroppedFrame] = []
        errors: list[BaseException] = []
        done = False

        def produce() -> None:
            """按原PTS推进，记录被覆盖的待处理帧；结束或异常都唤醒消费者。"""
            nonlocal done
            try:
                for frame in video.frames:
                    due = started + frame.timestamp_seconds - video.frames[0].timestamp_seconds
                    while not stop.is_set() and not cancel.cancelled:
                        remaining = due - monotonic()
                        if remaining <= 0:
                            break
                        stop.wait(min(remaining, 0.02))
                    if stop.is_set() or cancel.cancelled:
                        return
                    with condition:
                        if pending:
                            old, old_due = pending.pop()
                            dropped.append(
                                DroppedFrame(old.media, "superseded", monotonic() - old_due)
                            )
                        pending.append((frame, due))
                        condition.notify()
            except BaseException as error:
                with condition:
                    errors.append(error)
            finally:
                with condition:
                    done = True
                    condition.notify()

        observations: list[LiveObservation] = []
        worker = Thread(target=produce, name="adam-video-playback")
        worker.start()
        try:
            while True:
                cancel.check()
                with condition:
                    if errors:
                        raise errors[0]
                    if not pending:
                        if done:
                            break
                        condition.wait(0.02)
                        continue
                    frame, due = pending.pop()
                    before = monotonic()
                    age = before - due
                    if age > self.max_age_seconds:
                        dropped.append(DroppedFrame(frame.media, "expired", age))
                        continue
                # 回调不持有队列锁；慢分析不能阻塞真实播放，也不会积累无限旧帧。
                meaning = self.analyze_frame(frame, cancel)
                cancel.check()
                after = monotonic()
                if meaning.frame != frame.media:
                    raise ValueError("frame analysis evidence mismatch")
                if not meaning.label.strip() or len(meaning.label) > 1000 or not meaning.source:
                    raise ValueError("invalid frame analysis")
                observations.append(
                    LiveObservation(frame, meaning, age, after - before, after - due)
                )
        finally:
            stop.set()
            worker.join(timeout=1)
            if worker.is_alive():
                raise VideoError("cleanup", "Playback producer did not stop.")
        cancel.check()
        elapsed = monotonic() - started
        meanings = {item.frame.media.media_id: item.meaning for item in observations}
        sequence = TemporalAnalyzer(lambda f, c: meanings[f.media.media_id]).analyze(
            video, frames=tuple(item.frame for item in observations), cancel=cancel
        )
        return PlaybackReport(
            sequence,
            tuple(observations),
            tuple(dropped),
            elapsed,
            (len(video.frames) - 1) / span if span > 0 else None,
            len(observations) / elapsed if elapsed > 0 else None,
        )
