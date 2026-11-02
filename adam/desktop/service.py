"""完整快照经原媒体发布边界绑定真实来源。"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef, MediaSource
from adam.desktop.models import DesktopBackend, DesktopFrame, Observation


class DesktopObserver:
    """借用显式窗口后端，发布失败不返回成功观察。"""

    def __init__(
        self, backend: DesktopBackend, publish: Callable[[MediaSource, bytes, str], MediaRef]
    ) -> None:
        self.backend, self.publish = backend, publish

    def observe(self, source: MediaSource, cancel: CancellationToken) -> Observation:
        """树与截图共享 source；取消或后端失败不发布，不自动重试。"""
        cancel.check()
        frame = self.backend.observe(cancel)
        cancel.check()
        digest = frame_version(frame)
        image = self.publish(source, frame.png, "image/png")
        return Observation(digest, frame, image)


def frame_version(frame: DesktopFrame) -> str:
    """身份、树、几何和截图共同确定版本；像素变化也使旧观察失效。"""
    metadata = asdict(frame)
    metadata.pop("png")
    return hashlib.sha256(json.dumps(metadata, sort_keys=True).encode() + frame.png).hexdigest()
