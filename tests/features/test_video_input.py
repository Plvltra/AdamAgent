"""真实 Matroska/FFV1 解码与媒体来源验收，时间戳来自实际编码文件。"""

import io
from fractions import Fraction

import av
import pytest
from PIL import Image

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.video.adapters.pyav import PyAVDecoder
from adam.video.models import VideoError, VideoLimits
from adam.video.service import VideoLoader
from tests.features.test_media_evidence import active_media


def clip():
    """四个颜色帧按非等间隔PTS编码，用于验证真实可变帧率而非平均FPS推算。"""
    output = io.BytesIO()
    with av.open(output, "w", format="matroska") as container:
        stream = container.add_stream("ffv1", rate=1000)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "bgr0"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        for pts, color in zip((0, 100, 450, 900), ("red", "green", "blue", "yellow"), strict=True):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), color))
            frame.pts, frame.time_base = pts, Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return output.getvalue()


def test_real_vfr_timeline_and_published_frames(tmp_path):
    raw = clip()
    with active_media(tmp_path) as (store, source, media):
        result = VideoLoader(PyAVDecoder(VideoLimits(sample_seconds=0.05)), media.publish).load(
            source, raw, cancel=CancellationToken()
        )
        assert result.decoded_frames == 4
        assert [f.timestamp_seconds for f in result.frames] == pytest.approx([0, 0.1, 0.45, 0.9])
        assert media.read(result.video.media_id, expected_version=result.video.version).data == raw
        for frame in result.frames:
            assert frame.media.source == source
            assert (
                float(frame.pts * Fraction(frame.time_base_num, frame.time_base_den))
                == frame.timestamp_seconds
            )
            body = media.read(frame.media.media_id, expected_version=frame.media.version).data
            with Image.open(io.BytesIO(body)) as image:
                image.load()
                assert image.size == (64, 48)
        assert (
            len([e for e in store.load_events(source.session_id) if e["type"] == "media_saved"])
            == 5
        )


@pytest.mark.parametrize(
    "limits,kind",
    [
        (VideoLimits(max_seconds=0.2), "duration"),
        (VideoLimits(max_pixels=100), "resolution"),
        (VideoLimits(max_decoded_frames=2), "decode_limit"),
        (VideoLimits(max_samples=2, sample_seconds=0.05), "sample_limit"),
        (VideoLimits(max_sample_bytes=1), "sample_bytes"),
    ],
)
def test_limit_failure_never_publishes_partial_video(tmp_path, limits, kind):
    with active_media(tmp_path) as (store, source, media):
        with pytest.raises(VideoError) as error:
            VideoLoader(PyAVDecoder(limits), media.publish).load(
                source, clip(), cancel=CancellationToken()
            )
        assert error.value.kind == kind
        assert not any(e["type"] == "media_saved" for e in store.load_events(source.session_id))


def test_corrupt_media_is_not_an_empty_success():
    with pytest.raises(VideoError) as error:
        PyAVDecoder().decode(b"not a video", cancel=CancellationToken())
    assert error.value.kind == "corrupt"


def test_cancel_during_real_decode_has_no_published_media(tmp_path):
    class CancelDuringDecode(CancellationToken):
        def __init__(self):
            super().__init__()
            self.checks = 0

        def check(self):
            self.checks += 1
            if self.checks == 3:
                self.cancel()
            super().check()

    with active_media(tmp_path) as (store, source, media):
        with pytest.raises(RequestInterrupted):
            VideoLoader(PyAVDecoder(), media.publish).load(
                source, clip(), cancel=CancelDuringDecode()
            )
        assert not any(e["type"] == "media_saved" for e in store.load_events(source.session_id))
