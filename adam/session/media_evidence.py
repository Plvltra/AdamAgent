"""媒体文件与会话来源的可信宿主边界；模型不能凭 ID 访问其他会话或孤立文件。"""

from adam.contracts.errors import StorageError
from adam.contracts.media import MediaContent, MediaRef, MediaSource
from adam.session.media import MediaStore
from adam.session.storage import SessionStore


class SessionMedia:
    """正文发布后提交来源记录，读取时核对日志和文件中的完整不可变引用。"""

    def __init__(self, store: SessionStore, files: MediaStore) -> None:
        self.store, self.files = store, files

    def publish(self, source: MediaSource, data: bytes, mime_type: str) -> MediaRef:
        """先校验宿主来源，再保存正文和来源；任一步失败都不向调用者交付可读引用。

        IO 返回后复检所有者；日志提交失败可留下占用配额的孤立正文。
        孤立正文没有读取授权，重启不自动发布或重跑来源。
        """
        self._require_owner(source.session_id)
        try:
            self.store.validate_media_source(source)
        except ValueError as error:
            raise StorageError("媒体来源不是当前有效输入或工具，未保存。") from error
        reference = self.files.save(source, data, mime_type)
        self._require_owner(source.session_id)
        try:
            self.store.append_media(reference)
        except ValueError as error:
            raise StorageError("媒体来源记录校验失败，未交付引用；不要重做原动作。") from error
        return reference

    def read(self, media_id: str, *, expected_version: str) -> MediaContent:
        """仅读取宿主当前会话已提交的固定引用；缺失、损坏或篡改不寻找替代来源。"""
        session_id = self.store.active_session_id
        if session_id is None:
            raise StorageError("读取媒体前必须选中会话。")
        reference = self.store.load_media(session_id, media_id)
        if reference is None:
            raise StorageError("当前会话没有该媒体的已提交来源。")
        content = self.files.read(session_id, media_id, expected_version=expected_version)
        self._require_owner(session_id)
        if content.reference != reference:
            raise StorageError("媒体元数据与会话记录不符，拒绝读取。")
        return content

    def _require_owner(self, session_id: str) -> None:
        """只认宿主已选会话，发布参数不得隐式切换会话或取得另一把锁。"""
        if self.store.active_session_id != session_id:
            raise StorageError("媒体只允许发布到宿主当前会话，不自动切换所有者。")
