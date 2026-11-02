"""完整解码后复用媒体发布端口，不让失败短片产生看似完整的帧序列。"""

from collections.abc import Callable

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef, MediaSource
from adam.video.models import FrameRef, VideoDecoder, VideoFrames


class VideoLoader:
    """宿主传入已授权字节和真实来源；无文件路径、会话或模型SDK依赖。"""

    def __init__(
        self, decoder: VideoDecoder, publish: Callable[[MediaSource, bytes, str], MediaRef]
    ) -> None:
        self.decoder, self.publish = decoder, publish

    def load(self, source: MediaSource, data: bytes, *, cancel: CancellationToken) -> VideoFrames:
        """解码通过才发布原片及帧；发布失败或中途取消可留下部分证据，不重试。

        同一来源将原片与帧绑定；返回结果保留逐帧PTS，媒体文件各自通过版本校验。
        """
        decoded = self.decoder.decode(data, cancel=cancel)
        cancel.check()
        video = self.publish(source, data, decoded.mime_type)
        frames = []
        for frame in decoded.frames:
            cancel.check()
            ref = self.publish(source, frame.png, "image/png")
            frames.append(
                FrameRef(
                    frame.pts,
                    frame.time_base_num,
                    frame.time_base_den,
                    frame.timestamp_seconds,
                    frame.width,
                    frame.height,
                    ref,
                )
            )
        return VideoFrames(video, decoded.duration_seconds, decoded.decoded_frames, tuple(frames))
