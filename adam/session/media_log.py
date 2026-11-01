"""媒体来源的纯日志校验；不访问旁路正文，不把文件存在当成读取授权。"""

from typing import Any

from adam.contracts.media import MediaRef, MediaSource, decode_media_reference


class MediaLog:
    """保存已提交的完整引用；同一次工具可产生多份媒体，但 ID 不得重用。"""

    def __init__(self) -> None:
        self.references: dict[str, MediaRef] = {}

    def validate_source(
        self,
        source: MediaSource,
        session_id: str,
        turns: dict[str, dict[str, Any]],
        requests: dict[str, dict[str, Any]],
        tools: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        """只接受当前进行中轮次的输入，或成功请求下尚未结束的确切工具来源。"""
        if not isinstance(source, MediaSource) or source.session_id != session_id:
            raise ValueError("media session mismatch")
        turn = turns.get(source.turn_id)
        if turn is None or turn["status"] != "in_progress":
            raise ValueError("media requires an active turn")
        if source.kind == "input":
            if any(request["turn_id"] == source.turn_id for request in requests.values()):
                raise ValueError("input media must precede model requests")
            return
        request = requests.get(source.request_id or "")
        tool = tools.get((source.turn_id, source.tool_call_id or ""))
        if (
            request is None
            or request["turn_id"] != source.turn_id
            or request["status"] != "succeeded"
            or tool is None
            or tool["request_id"] != source.request_id
            or tool["name"] != source.tool_name
            or tool["finished"]
        ):
            raise ValueError("media requires a live matching tool")

    def apply(
        self,
        payload: dict[str, Any],
        session_id: str,
        turns: dict[str, dict[str, Any]],
        requests: dict[str, dict[str, Any]],
        tools: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        """原子追加前校验完整引用；重复 ID、无效所有者和已结束来源均拒绝。"""
        reference = decode_media_reference(payload)
        self.validate_source(reference.source, session_id, turns, requests, tools)
        if reference.media_id in self.references:
            raise ValueError("duplicate media identity")
        self.references[reference.media_id] = reference
