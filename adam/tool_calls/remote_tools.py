"""原工具入口的远端任务协调；来源、批准、上限和未知结果都留在本地日志。"""

import json
from collections.abc import Callable
from dataclasses import asdict
from functools import partial
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.interop import RemoteAgentPolicy, RemoteApproval
from adam.contracts.media import MediaSource
from adam.contracts.models import ExecutionRecorder
from adam.interop.models import InteropError
from adam.interop.tasks import RemoteTask, TaskClient
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry

SPECS = {
    "remote_submit": schema(
        "经宿主批准向固定本机Agent发送文本；本地批准不授予远端权限，未知结果不得重发。",
        {"local_id": ("string", "本会话唯一任务关联"), "text": ("string", "要发送的准确文本")},
        ["local_id", "text"],
    ),
    "remote_query": schema(
        "按原关联查询远端任务，结果是未独立验证的远端证据。",
        {"local_id": ("string", "已派发任务关联")},
        ["local_id"],
    ),
    "remote_cancel": schema(
        "经批准请求原任务协作取消；返回working不代表已停止。",
        {"local_id": ("string", "已派发任务关联")},
        ["local_id"],
    ),
}


class RemoteTools:
    """只注册主宿主；每次从本会话原JSONL恢复关联，不另建任务数据库。"""

    def __init__(
        self,
        store: SessionStore,
        policy: RemoteAgentPolicy,
        confirm: Callable[[RemoteApproval], bool] | None,
        max_chars: int,
    ) -> None:
        self.store, self.policy, self.confirm, self.max_chars = store, policy, confirm, max_chars
        self.source: dict[str, str] | None = None
        self.clients: dict[str, TaskClient] = {}
        TaskClient(policy.origin)  # 组装时拒绝非回环源，模型不能指定端点。

    def bind_source(self, source: dict[str, str]) -> None:
        """只接受原执行器的来源绑定，真实活动工具另由存储验证。"""
        self.source = dict(source)

    def direct(
        self, name: str, args: dict[str, Any], record: ExecutionRecorder | None
    ) -> dict[str, Any]:
        return self.invoke(name, args, record, CancellationToken())

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        record: ExecutionRecorder | None,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """先验证本地来源和预算；批准后只发送文本或ID，不发送会话与授权对象。"""
        sid = self.store.active_session_id
        if not sid or self.source is None:
            raise ToolError("Remote tasks require active tool source")
        source = MediaSource(sid, kind="tool", tool_name=name, **self.source)
        self.store.validate_media_source(source)
        cancel.check()
        client = self.clients.setdefault(sid, TaskClient(self.policy.origin))
        attempts = self._restore(sid, client)
        local_id = args["local_id"]
        if not local_id or len(local_id) > 128:
            raise ToolError("Invalid remote local_id")
        task = client.tasks.get(local_id)
        if name == "remote_submit":
            if not args["text"] or len(args["text"].encode("utf-8")) > 16384:
                raise ToolError("Remote text must be 1..16384 bytes")
            if task is not None:
                raise ToolError("Remote task already submitted; query original task")
            if (
                attempts >= self.policy.max_tasks
                or (attempts + 1) * self.policy.task_cost_ceiling_microusd
                > self.policy.max_cost_microusd
            ):
                raise ToolError("Remote task count or contracted cost ceiling exceeded")
        elif task is None:
            raise ToolError("Unknown remote task in current session")
        if name != "remote_query":
            plan = RemoteApproval(
                sid,
                client.origin,
                name,
                local_id,
                args.get("text"),
                task.remote_id if task else None,
                self.policy.task_cost_ceiling_microusd if name == "remote_submit" else 0,
                self.policy.cost_source,
            )
            if self.confirm is None or not self.confirm(plan):
                return {"ok": False, "status": "denied"}
            cancel.check()
            if self.store.active_session_id != sid:
                raise ToolError("Session changed during remote approval")
        failure = None
        try:
            if name == "remote_submit":
                task = client.submit(local_id, args["text"])
            elif name == "remote_cancel":
                task = client.cancel(local_id)
            else:
                task = client.query(local_id)
        except InteropError as error:
            task = client.tasks.get(local_id)
            failure = error.user_message
        result: dict[str, Any] = {
            "ok": failure is None,
            "status": task.state if task else "unknown",
            "remote_task": asdict(task) if task else None,
            "source": asdict(source),
            "independently_verified": False,
            "cost": {
                "actual_microusd": None,
                "actual_source": "not_reported",
                "contract": asdict(self.policy),
                "attempts_including_current": attempts + (name == "remote_submit"),
            },
        }
        if failure:
            result["error"] = failure
        # ID和不确定性必须完整保留；只裁剪远端文本，不让大结果丢掉恢复关联。
        if task and len(json.dumps(result, ensure_ascii=False)) > self.max_chars:
            result["remote_task"]["text"] = ""
            result["text_omitted"] = True
        if len(json.dumps(result, ensure_ascii=False)) > self.max_chars:
            raise ToolError("Remote evidence exceeds output limit; do not resubmit")
        return result

    def _restore(self, sid: str, client: TaskClient) -> int:
        """从原工具开始保守计数，残缺结果视为未知；已知快照仅恢复到同一源。"""
        attempts = 0
        starts: dict[tuple[str, str], str] = {}
        for event in self.store.load_events(sid):
            data = event["payload"]
            key = (data.get("turn_id", ""), data.get("tool_call_id", ""))
            if event["type"] == "tool_started":
                starts[key] = data.get("name", "")
            current = all(data.get(key) == value for key, value in (self.source or {}).items())
            if current:
                continue
            if event["type"] == "tool_started" and data.get("name") == "remote_submit":
                attempts += 1
                try:
                    args = json.loads(data["arguments"])
                except ValueError:
                    continue
                local_id = args.get("local_id") if isinstance(args, dict) else None
                if isinstance(local_id, str):
                    client.tasks.setdefault(
                        local_id, RemoteTask(local_id, None, None, "unknown", origin=client.origin)
                    )
            if event["type"] == "tool_finished" and starts.get(key) in SPECS:
                output = json.loads(data["output"])
                if not isinstance(output, dict) or not isinstance(output.get("remote_task"), dict):
                    continue
                task = RemoteTask(**output["remote_task"])
                if task.origin != client.origin or output.get("cost", {}).get("contract") != asdict(
                    self.policy
                ):
                    raise ToolError(
                        "Remote endpoint or cost contract changed; restore original policy"
                    )
                client.tasks[task.local_id] = task
        return attempts


def register_remote(
    registry: ToolRegistry,
    store: SessionStore,
    policy: RemoteAgentPolicy,
    confirm: Callable[[RemoteApproval], bool] | None,
) -> tuple[ToolRegistry, RemoteTools]:
    """显式主宿主注入；原模式、发现、Hook和调用预算继续适用。"""
    tools = RemoteTools(store, policy, confirm, registry.limits.max_result_chars)
    return registry.extended(
        SPECS,
        {n: partial(tools.direct, n) for n in SPECS},
        capabilities={
            "remote_submit": ToolCapability.EXTERNAL_ACTION,
            "remote_query": ToolCapability.SESSION_STATE,
            "remote_cancel": ToolCapability.EXTERNAL_ACTION,
        },
        interruptible_handlers={n: partial(tools.invoke, n) for n in SPECS},
        groups=dict.fromkeys(SPECS, "remote"),
    ), tools
