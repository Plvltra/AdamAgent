"""录制媒体的独立冻结副本；固定旁路目录，不执行 JSON 提供的路径或外部 URL。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.contracts.errors import AgentError
from adam.contracts.media import MediaContent, MediaRef
from adam.contracts.model_input import message_media
from adam.recording.format import RecordingError
from adam.session.media import MediaStore

MediaReader = Callable[[MediaRef], MediaContent]


class MediaArchive:
    """媒体正文只进入旁路文件；JSONL 保留原引用，缺少来源或字节不能冒充可重放。"""

    def __init__(self, recording: Path) -> None:
        self.files = MediaStore(recording.with_name(recording.name + ".media"))

    def capture(self, references: tuple[MediaRef, ...], reader: MediaReader | None) -> None:
        """在模型请求前冻结已授权的完整证据；失败不请求模型、不丢图改发文字。"""
        if references and reader is None:
            raise RecordingError("录制没有媒体读取端口，未发送模型请求。")
        for reference in references:
            assert reader is not None
            try:
                content = reader(reference)
                if not isinstance(content, MediaContent) or content.reference != reference:
                    raise ValueError("media source changed")
                self.files.restore(content)
            except (AgentError, OSError, ValueError, TypeError) as error:
                raise RecordingError("媒体冻结失败；未发送模型请求，未自动重试。") from error

    def verify(self, references: tuple[MediaRef, ...]) -> None:
        """先检查归档累计配额，再只读验证字节、来源、时间与摘要。

        不访问原会话、网络或原媒体目录，不创建锁文件或修复副本。
        """
        counts: dict[str, int] = {}
        sizes: dict[str, int] = {}
        for reference in references:
            owner = reference.source.session_id
            counts[owner] = counts.get(owner, 0) + 1
            sizes[owner] = sizes.get(owner, 0) + reference.size_bytes
            if (
                reference.size_bytes > self.files.max_bytes
                or counts[owner] > self.files.max_items
                or sizes[owner] > self.files.max_session_bytes
            ):
                raise RecordingError("录制媒体数量或字节超过归档读取上限，未读取正文。")
        for reference in references:
            try:
                self.files.read_frozen(reference)
            except (AgentError, OSError, ValueError, TypeError) as error:
                raise RecordingError("录制媒体副本缺失、损坏或来源不符，不能重放。") from error


def request_media(messages: list[dict[str, Any]]) -> tuple[MediaRef, ...]:
    """按请求顺序取得全部公共媒体，包含历史；不解释工具正文里的引用 JSON。"""
    return tuple(reference for message in messages for reference in message_media(message))
