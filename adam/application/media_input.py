"""主/子会话的媒体输入预检；只消费已提交引用，不上传路径或隐式扩大读取权限。"""

from adam.contracts.errors import AgentError
from adam.contracts.media import MediaContent, MediaRef
from adam.contracts.model_input import declared_capabilities, media_records
from adam.contracts.ports import ModelClient
from adam.session.media_evidence import SessionMedia


class MediaInputService:
    """在登记本轮前核对声明、所有者和实际字节；发送边界仍须再次验证版本。"""

    def __init__(self, media: SessionMedia, client: ModelClient) -> None:
        self._media, self._client = media, client

    def validate(self, references: tuple[MediaRef, ...], model: str) -> None:
        """不支持或读取失败时不登记输入、不请求模型，不丢弃附件改发文字。"""
        media_records(references)
        declared_capabilities(self._client, model).require_media(references, model)
        for reference in references:
            self.read(reference)

    def read(self, reference: MediaRef) -> MediaContent:
        """只认当前会话已提交的完整引用；同摘要但来源不同的文件也不能替代。"""
        content = self._media.read(reference.media_id, expected_version=reference.version)
        if content.reference != reference:
            raise AgentError("媒体引用与当前会话实际来源不符，未发送请求。")
        return content
