"""A2A 任务关联与查询；连接失败不会转成重新委派。"""

from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from adam.interop.client import TextAgentClient
from adam.interop.models import InteropError

STATES = {
    "submitted",
    "working",
    "input-required",
    "auth-required",
    "completed",
    "canceled",
    "failed",
    "rejected",
    "unknown",
}


@dataclass(frozen=True)
class RemoteTask:
    """本地关联与远端 ID；unknown 表示当前无法确认，不覆盖最后已知状态。"""

    local_id: str
    remote_id: str | None
    context_id: str | None
    state: str
    text: str = ""
    last_known_state: str | None = None
    origin: str = ""


class TaskClient(TextAgentClient):
    """单宿主任务关联；宿主可保存不可变快照，重建后显式 attach 再查询。"""

    def __init__(self, origin: str, *, timeout: float = 10.0) -> None:
        super().__init__(origin, timeout=timeout)
        self.tasks: dict[str, RemoteTask] = {}

    def submit(self, local_id: str, text: str) -> RemoteTask:
        """首次派发前保留关联；同一关联拒绝再次发送，包括首次响应丢失。"""
        if not local_id or len(local_id) > 128 or local_id in self.tasks:
            raise InteropError("本地关联无效或已经派发；请查询原任务。")
        if not text or len(text.encode("utf-8")) > 16384:
            raise InteropError("文本必须为 1～16384 字节。")
        self.discover()
        self.tasks[local_id] = RemoteTask(local_id, None, None, "unknown", origin=self.origin)
        result = self._rpc(
            "message/send",
            {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": uuid4().hex,
                    "parts": [{"kind": "text", "text": text}],
                },
                "configuration": {"blocking": False, "acceptedOutputModes": ["text/plain"]},
            },
        )
        task = self._decode(local_id, result)
        self.tasks[local_id] = task
        return task

    def attach(self, task: RemoteTask) -> None:
        """恢复宿主保存的同一源关联；只登记，不发送任务或自动恢复动作。"""
        if (
            task.origin != self.origin
            or task.local_id in self.tasks
            or not task.local_id
            or (
                (task.remote_id is None or task.context_id is None)
                and not (
                    task.remote_id is None and task.context_id is None and task.state == "unknown"
                )
            )
        ):
            raise InteropError("不能恢复异源、无效或已存在的关联。")
        self.tasks[task.local_id] = task

    def query(self, local_id: str) -> RemoteTask:
        """查询原远端 ID；连接恢复后仍查询原任务，不重新 submit。"""
        return self._update(local_id, "tasks/get")

    def cancel(self, local_id: str) -> RemoteTask:
        """请求协作取消；以远端返回状态为准，不把请求成功当作已停止。"""
        return self._update(local_id, "tasks/cancel")

    def _update(self, local_id: str, method: str) -> RemoteTask:
        previous = self.tasks.get(local_id)
        if previous is None or previous.remote_id is None:
            raise InteropError("远端任务 ID 未知；不能查询或盲目重发。")
        try:
            task = self._decode(local_id, self._rpc(method, {"id": previous.remote_id}))
            if task.remote_id != previous.remote_id or task.context_id != previous.context_id:
                raise InteropError("远端任务关联改变。")
        except InteropError:
            self.tasks[local_id] = replace(
                previous,
                state="unknown",
                last_known_state=previous.last_known_state or previous.state,
            )
            raise
        self.tasks[local_id] = task
        return task

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """一次 JSON-RPC 请求；协议失败保留已有远端关联。"""
        request_id = uuid4().hex
        response = self._request(
            "POST", "/", {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        if (
            response.get("jsonrpc") != "2.0"
            or response.get("id") != request_id
            or "error" in response
            or not isinstance(response.get("result"), dict)
        ):
            raise InteropError("远端任务协议失败；未重新派发。")
        return response["result"]

    def _decode(self, local_id: str, data: dict[str, Any]) -> RemoteTask:
        """只接受规范任务和文本产物；不把陌生状态转换为成功。"""
        status = data.get("status")
        if (
            data.get("kind") != "task"
            or not isinstance(data.get("id"), str)
            or not data["id"]
            or not isinstance(data.get("contextId"), str)
            or not data["contextId"]
            or not isinstance(status, dict)
            or not isinstance(status.get("state"), str)
            or status["state"] not in STATES
        ):
            raise InteropError("远端 Task 字段无效。")
        texts: list[str] = []
        artifacts = data.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise InteropError("远端任务产物无效。")
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("parts"), list):
                raise InteropError("远端任务产物无效。")
            for part in artifact["parts"]:
                if (
                    not isinstance(part, dict)
                    or part.get("kind") != "text"
                    or not isinstance(part.get("text"), str)
                ):
                    raise InteropError("任务客户端只支持文本产物。")
                texts.append(part["text"])
        return RemoteTask(
            local_id,
            data["id"],
            data["contextId"],
            status["state"],
            "\n".join(texts),
            origin=self.origin,
        )
