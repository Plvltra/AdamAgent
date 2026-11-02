"""B32决策前快照与动作集合验收；只重选下一步，不自动执行模型选择的工具。"""

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adam.contracts.models import Completion, ModelOutput
from adam.contracts.ports import ModelClient
from adam.contracts.protocol import validate_assistant
from adam.recording.replay import first_difference


@dataclass(frozen=True)
class SnapshotRef:
    """宿主持有的快照路径与原始哈希；不能以当前文件重新计算的哈希替换期望值。"""

    path: Path
    sha256: str


class DecisionCapture:
    """模型调用前保存准确请求及有界工作区文件；捕获失败则不请求远端。

    每个实例使用新目录，适用于小型练习工作区，不是任意项目的备份系统。
    记录业务消息原文，不能用来分享未脱敏的真实用户内容。
    """

    def __init__(self, client: ModelClient, workspace: Path, output: Path) -> None:
        self.client, self.workspace = client, workspace.resolve()
        self.output = output.absolute()
        if self.output.is_relative_to(self.workspace):
            raise ValueError("snapshot directory must be outside workspace")
        self.output.mkdir(parents=True, exist_ok=False)
        self.snapshots: list[SnapshotRef] = []

    def list_models(self) -> dict[str, str | None]:
        """目录查询不属于决策前缀，直接透传。"""
        return self.client.list_models()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """保存请求前文件状态，再原样请求一次；不会事后拿变化后的文件冒充前缀。"""
        value = {
            "format": "adam_decision_prefix_v1",
            "workspace": str(self.workspace),
            "request": {
                "messages": messages,
                "model": model,
                "tools": tools,
                "max_tokens": max_tokens,
            },
            "files": _capture_files(self.workspace),
        }
        path = self.output / f"decision-{len(self.snapshots) + 1:04}.json"
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
        reference = SnapshotRef(path, hashlib.sha256(raw).hexdigest())
        self.snapshots.append(reference)
        return self.client.complete(
            messages, model=model, tools=tools, max_tokens=max_tokens, on_output=on_output
        )


def restore(reference: SnapshotRef, destination: Path) -> dict[str, Any]:
    """核对原哈希后在全新目录恢复文件；不覆盖原工作区，不执行任何历史动作。"""
    value = load(reference)
    destination = destination.absolute()
    if destination.resolve() != destination or destination == Path(value["workspace"]):
        raise ValueError("restore target must be a new independent directory")
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in value["files"].items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(content, validate=True))
    return value


def choose_next(
    reference: SnapshotRef,
    client: ModelClient,
    allowed: tuple[dict[str, Any], ...],
    forbidden: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """用冻结请求重新选择一步，再按动作集合验收；只调用一次模型，绝不执行所选工具。"""
    value = load(reference)
    completion = client.complete(**value["request"])
    return {
        "mode": "new_decision",
        "snapshot_sha256": reference.sha256,
        "response": completion.message,
        "usage": completion.usage,
        "verification": verify_actions(completion.message, allowed, forbidden),
    }


def verify_actions(
    message: dict[str, Any],
    allowed: tuple[dict[str, Any], ...],
    forbidden: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """每个动作都须匹配至少一条允许规则且不匹配禁止规则；可有多个合法下一步。

    规则包含name，可选arguments子集；参数使用既有严格类型比较，禁止规则优先。
    """
    validate_assistant(message)
    for rule in (*allowed, *forbidden):
        if not isinstance(rule.get("name"), str) or not isinstance(rule.get("arguments", {}), dict):
            raise ValueError("invalid action rule")
    calls = message.get("tool_calls") or []
    actions = (
        [
            {"name": c["function"]["name"], "arguments": json.loads(c["function"]["arguments"])}
            for c in calls
        ]
        if calls
        else [{"name": "final_answer", "arguments": {"text": message.get("content")}}]
    )
    rejected = [
        a
        for a in actions
        if any(_matches(a, r) for r in forbidden) or not any(_matches(a, r) for r in allowed)
    ]
    return {"passed": not rejected, "actions": actions, "rejected": rejected}


def load(reference: SnapshotRef) -> dict[str, Any]:
    """拒绝修改过的前缀、非法文件路径与超限内容；哈希不是未知来源的信任凭据。"""
    raw = reference.path.read_bytes()
    if len(raw) > 8_000_000 or hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ValueError("snapshot changed or exceeds limit")
    value = json.loads(raw)
    if value.get("format") != "adam_decision_prefix_v1" or not isinstance(value.get("files"), dict):
        raise ValueError("invalid snapshot")
    total = 0
    for name, content in value["files"].items():
        path = Path(name)
        if path.is_absolute() or path.drive or ".." in path.parts or not name or ":" in name:
            raise ValueError("invalid snapshot file path")
        data = base64.b64decode(content, validate=True)
        total += len(data)
        if len(data) > 1_000_000 or total > 2_000_000 or len(value["files"]) > 100:
            raise ValueError("snapshot files exceed limits")
    return value


def _capture_files(workspace: Path) -> dict[str, str]:
    result, total = {}, 0
    for index, path in enumerate(workspace.rglob("*")):
        if index >= 1000 or path.resolve() != path.absolute():
            raise ValueError("workspace is too large or redirects through links")
        if path.is_file():
            size = path.stat().st_size
            if size > 1_000_000 or total + size > 2_000_000 or len(result) >= 100:
                raise ValueError("workspace snapshot exceeds limits")
            data = path.read_bytes()
            total += len(data)
            result[path.relative_to(workspace).as_posix()] = base64.b64encode(data).decode("ascii")
    return result


def _matches(action: dict[str, Any], rule: dict[str, Any]) -> bool:
    return action["name"] == rule["name"] and all(
        key in action["arguments"] and first_difference(value, action["arguments"][key]) is None
        for key, value in rule.get("arguments", {}).items()
    )
