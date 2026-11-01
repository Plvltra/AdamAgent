"""从当前会话已保存工具结果回查网页来源；不创建第二份证据数据库。"""

import json
import re
from typing import Any

from adam.contracts.models import ExecutionRecorder
from adam.session.storage import SessionStore
from adam.web.evidence import quote_page
from adam.web.models import WebError


class WebSources:
    """只借用所属主/子存储；回查在协调线程串行执行，不共享活动会话状态。"""

    def __init__(self, store: SessionStore, max_chars: int) -> None:
        self._store = store
        self._max_chars = max_chars

    def read(self, args: dict[str, Any], record: ExecutionRecorder | None = None) -> dict[str, Any]:
        """只读当前会话的真实 read_webpage 完成结果；失败或缺失不重新抓取。"""
        try:
            identity = args["source_id"]
            if not isinstance(identity, str) or re.fullmatch(r"web-[0-9a-f]{64}", identity) is None:
                raise WebError("invalid_source", "网页来源 ID 无效，不接受 URL、路径或会话参数。")
            current = self._store.active_session_id
            if current is None:
                raise WebError("no_session", "回查网页来源前必须选中当前会话。")
            saved = self._find(current, identity)
            output = quote_page(
                saved,
                start=args.get("start", 0),
                count=args.get("count", 1000),
                max_chars=self._max_chars,
            )
            if self._store.active_session_id != current:
                raise WebError("session_changed", "当前会话已变化，未交付旧会话网页片段。")
            return output
        except WebError as error:
            return {"ok": False, "status": "failed", "error_type": error.kind, "error": str(error)}

    def _find(self, session_id: str, identity: str) -> dict[str, Any]:
        """先核对日志关联，再解析原读取输出；其他工具中相同字段不能冒充抓取来源。"""
        starts: set[tuple[str, str, str]] = set()
        for event in self._store.load_events(session_id):
            payload = event["payload"]
            key = (
                str(payload.get("turn_id", "")),
                str(payload.get("request_id", "")),
                str(payload.get("tool_call_id", "")),
            )
            if event["type"] == "tool_started" and payload["name"] == "read_webpage":
                starts.add(key)
            elif event["type"] == "tool_finished" and key in starts:
                try:
                    result = json.loads(payload["output"])
                except (ValueError, TypeError, RecursionError):
                    continue
                if isinstance(result, dict) and result.get("source_id") == identity:
                    return result
        raise WebError(
            "source_not_found", "当前会话没有此网页快照；缺失来源不会自动重新搜索或抓取。"
        )
