"""完整快照经原媒体发布边界绑定真实来源。"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef, MediaSource
from adam.desktop.models import DesktopBackend, Observation


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
        metadata = asdict(frame)
        metadata.pop("png")
        digest = hashlib.sha256(
            json.dumps(metadata, sort_keys=True).encode() + frame.png
        ).hexdigest()
        image = self.publish(source, frame.png, "image/png")
        return Observation(digest, frame, image)
