"""时序规则使用显式帧语义端口；不把替身标签当作真实视觉能力验收。"""

from dataclasses import replace
from hashlib import sha256

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.contracts.media import MediaRef, MediaSource
from adam.video.models import FrameRef, VideoFrames
from adam.video.temporal import FrameMeaning, TemporalAnalyzer


def sequence():
    source = MediaSource("a" * 8, "b" * 32, "input")

    def ref(name):
        return MediaRef(
            sha256(name.encode()).hexdigest()[:32],
            source,
            "image/png",
            10,
            "a" * 64,
            "2026-11-02T00:00:00+00:00",
        )

    frames = tuple(FrameRef(i, 1, 2, i / 2, 64, 48, ref(f"frame{i}")) for i in range(4))
    return VideoFrames(replace(ref("video"), mime_type="video/mp4"), 2, 4, frames)


def analyze(frame, cancel):
    return FrameMeaning(
        frame.media, "closed" if frame.pts < 2 else "open", False, "test-frame-port"
    )


def test_event_segments_keep_time_and_all_evidence():
    report = TemporalAnalyzer(analyze).analyze(sequence(), cancel=CancellationToken())
    assert report.order_known and report.issues == ()
    assert [(e.label, e.start_seconds, e.end_seconds) for e in report.events] == [
        ("closed", 0, 0.5),
        ("open", 1, 1.5),
    ]
    assert [len(e.frames) for e in report.events] == [2, 2]
    assert report.coverage == "sampled_frames_only"


def test_shuffled_frames_refuse_certain_order_before_analysis():
    video = sequence()
    called = []
    report = TemporalAnalyzer(lambda f, c: called.append(f)).analyze(
        video, frames=video.frames[::-1], cancel=CancellationToken()
    )
    assert not report.order_known and report.events == ()
    assert report.issues == ("out_of_order_or_duplicate",) and called == []


def test_missing_frames_do_not_keep_same_certainty_or_merge_across_gap():
    video = sequence()
    report = TemporalAnalyzer(analyze).analyze(
        video,
        frames=(video.frames[0], video.frames[2], video.frames[3]),
        cancel=CancellationToken(),
    )
    assert not report.order_known and "missing_samples" in report.issues
    assert all(event.uncertain for event in report.events)
    assert report.analyzed_samples == 3 and report.expected_samples == 4


def test_uncertain_meaning_and_wrong_evidence_are_not_confident_events():
    video = sequence()
    report = TemporalAnalyzer(lambda f, c: replace(analyze(f, c), uncertain=True)).analyze(
        video, cancel=CancellationToken()
    )
    assert not report.order_known and "uncertain_frame_analysis" in report.issues
    with pytest.raises(ValueError, match="evidence mismatch"):
        TemporalAnalyzer(lambda f, c: replace(analyze(f, c), frame=video.video)).analyze(
            video, cancel=CancellationToken()
        )


def test_cancellation_does_not_run_remaining_frame_analysis():
    token, called = CancellationToken(), []

    def cancel_after_one(frame, cancel):
        called.append(frame)
        cancel.cancel()
        return analyze(frame, cancel)

    with pytest.raises(RequestInterrupted):
        TemporalAnalyzer(cancel_after_one).analyze(sequence(), cancel=token)
    assert len(called) == 1
