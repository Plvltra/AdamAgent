"""实时播放使用真实时钟和线程；语义回调是替身，不冒充视觉模型验收。"""

from dataclasses import replace
from threading import enumerate as threads
from time import sleep

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.video.live import RealtimePlayback
from adam.video.models import VideoError
from tests.features.test_video_temporal import analyze, sequence


def short_video():
    video = sequence()
    return replace(
        video,
        frames=tuple(
            replace(frame, time_base_den=20, timestamp_seconds=frame.pts / 20)
            for frame in video.frames
        ),
    )


def test_playback_preserves_evidence_without_extra_analysis():
    video = sequence()
    video = replace(video, frames=video.frames[:1])
    calls = []

    def inspect(frame, cancel):
        calls.append(frame)
        return analyze(frame, cancel)

    report = RealtimePlayback(inspect).observe(video, cancel=CancellationToken())
    assert calls == list(video.frames)
    assert report.sequence.order_known and not report.dropped
    assert report.observations[0].meaning.frame == video.frames[0].media
    assert report.observations[0].latency_seconds >= report.observations[0].analysis_seconds
    assert report.source_kind == "realtime_video_playback"


def test_slow_analysis_drops_backlog_and_keeps_missing_evidence_uncertain():
    video = short_video()

    def slow(frame, cancel):
        sleep(0.25)
        return analyze(frame, cancel)

    report = RealtimePlayback(slow, max_age_seconds=2).observe(video, cancel=CancellationToken())
    observed = {item.frame.media.media_id for item in report.observations}
    dropped = {item.frame.media_id for item in report.dropped}
    assert observed.isdisjoint(dropped)
    assert observed | dropped == {frame.media.media_id for frame in video.frames}
    assert report.dropped and all(item.reason == "superseded" for item in report.dropped)
    assert "missing_samples" in report.sequence.issues
    assert all(event.uncertain for event in report.sequence.events)
    assert report.observations[-1].frame == video.frames[-1]


def test_old_pending_frame_is_not_analyzed_after_slow_callback():
    def slow(frame, cancel):
        sleep(0.3)
        return analyze(frame, cancel)

    report = RealtimePlayback(slow, max_age_seconds=0.01).observe(
        short_video(), cancel=CancellationToken()
    )
    assert any(item.reason == "expired" for item in report.dropped)
    assert all(item.queue_seconds <= 0.01 for item in report.observations)


@pytest.mark.parametrize("failure", ["cancel", "backend", "evidence"])
def test_failure_stops_producer_without_retry(failure):
    token, calls = CancellationToken(), []

    def fail(frame, cancel):
        calls.append(frame)
        if failure == "cancel":
            cancel.cancel()
        elif failure == "backend":
            raise RuntimeError("injected analysis failure")
        result = analyze(frame, cancel)
        return replace(result, frame=sequence().video) if failure == "evidence" else result

    expected = {"cancel": RequestInterrupted, "backend": RuntimeError, "evidence": ValueError}
    with pytest.raises(expected[failure]):
        RealtimePlayback(fail).observe(sequence(), cancel=token)
    assert len(calls) == 1
    assert not any(thread.name == "adam-video-playback" for thread in threads())


def test_invalid_timeline_and_pre_cancel_do_not_start_analysis():
    called = []
    player = RealtimePlayback(lambda f, c: called.append(f))
    video = sequence()
    with pytest.raises(VideoError, match="timeline"):
        player.observe(replace(video, frames=video.frames[::-1]), cancel=CancellationToken())
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RequestInterrupted):
        player.observe(video, cancel=token)
    assert called == []
