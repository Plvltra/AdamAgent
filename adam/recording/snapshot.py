"""录制所需的运行快照；只选择流程配置，不读取连接凭据或外部服务环境。"""

from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from adam.config.settings import (
    AgentConfig,
    AppConfig,
    ArtifactConfig,
    ContextConfig,
    MemoryConfig,
    NoProgressConfig,
    PlanningConfig,
    ReflectionConfig,
    ReviewConfig,
    SubagentConfig,
    ToolLimits,
)
from adam.contracts.errors import ConfigError
from adam.contracts.execution import ExecutionMode
from adam.contracts.models import valid_session_id
from adam.session.log import SessionLog
from adam.session.storage import SessionStore

# 明确选择不含凭据的值对象；禁止对整个 AppConfig 做 asdict。
CONTROL_TYPES = {
    "agent": AgentConfig,
    "context": ContextConfig,
    "tool_limits": ToolLimits,
    "memory": MemoryConfig,
    "planning": PlanningConfig,
    "reflection": ReflectionConfig,
    "review": ReviewConfig,
    "subagents": SubagentConfig,
    "artifacts": ArtifactConfig,
    "no_progress": NoProgressConfig,
}


def capture_seed(
    config: AppConfig,
    store: SessionStore,
    session_id: str,
    user_text: str,
    workspace: Path,
    *,
    model: str | None = None,
    mode: ExecutionMode = ExecutionMode.ACT,
) -> dict[str, Any]:
    """取得所选轮次开始前的历史与流程配置；调用方须持有当前会话锁。

    返回 JSON 值副本，不保存 api_key、base_url、HTTP 头、Hook 命令或 MCP 环境。
    业务消息原文保留；这不是对用户内容中任意秘密的自动脱敏器。
    """
    if store.active_session_id != session_id:
        raise ValueError("录制必须绑定当前已选会话。")
    seed = {
        "session_id": session_id,
        "user_text": user_text,
        "model": model if model is not None else config.llm.model,
        "workspace": str(workspace.resolve()),
        "mode": mode.value,
        "stream": config.llm.stream,
        "controls": {name: asdict(getattr(config, name)) for name in CONTROL_TYPES},
        "session_events": store.load_events(session_id),
    }
    validate_seed(seed)
    return seed


def validate_seed(seed: object) -> None:
    """验证重放起点及配置白名单；保留未知消费和历史身份，不执行配置中的任何内容。"""
    keys = {
        "session_id",
        "user_text",
        "model",
        "workspace",
        "mode",
        "stream",
        "controls",
        "session_events",
    }
    if not isinstance(seed, dict) or seed.keys() != keys:
        raise ValueError("invalid recording seed")
    if not isinstance(seed["session_id"], str) or not valid_session_id(seed["session_id"]):
        raise ValueError("invalid recording session")
    if any(not isinstance(seed[key], str) for key in ("user_text", "model", "workspace")):
        raise ValueError("invalid recording input")
    if not seed["model"].strip() or not Path(seed["workspace"]).is_absolute():
        raise ValueError("invalid model or workspace")
    if seed["mode"] not in {"act", "plan"} or type(seed["stream"]) is not bool:
        raise ValueError("invalid mode or stream")
    controls = seed["controls"]
    # 早期录制没有检测配置；重放读取已保存结果，不以今天的默认策略重做旧工具。
    required = CONTROL_TYPES.keys() - {"no_progress"}
    if not isinstance(controls, dict) or not required <= controls.keys() <= CONTROL_TYPES.keys():
        raise ValueError("invalid recording controls")
    for name, values in controls.items():
        cls = CONTROL_TYPES[name]
        if not isinstance(values, dict) or set(values) != {field.name for field in fields(cls)}:
            raise ValueError("invalid recording control fields")
        defaults = asdict(cls())
        for key, value in values.items():
            expected = defaults[key]
            if isinstance(expected, bool):
                valid = type(value) is bool
            elif isinstance(expected, int):
                valid = type(value) is int and 0 <= value <= 1_000_000_000
            else:
                valid = type(value) in {int, float}
            if not valid:
                raise ValueError("invalid recording control value")
        try:
            cls(**values)
        except (ConfigError, TypeError, ValueError):
            raise ValueError("invalid recording controls") from None
    if any(value <= 0 for value in controls["agent"].values()):
        raise ValueError("invalid recording agent budget")
    events = seed["session_events"]
    if not isinstance(events, list) or not events:
        raise ValueError("missing session seed")
    meta = events[0]
    if (
        not isinstance(meta, dict)
        or meta.get("type") != "session_meta"
        or not isinstance(meta.get("payload"), dict)
        or meta["payload"].get("version") != 4
        or meta["payload"].get("id") != seed["session_id"]
        or not all(isinstance(meta["payload"].get(key), str) for key in ("title", "system_prompt"))
    ):
        raise ValueError("invalid seed metadata")
    try:
        log = SessionLog(meta["payload"])
        log.extend(events[1:])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("invalid seed history") from None
    if any(turn["status"] == "in_progress" for turn in log.turns.values()):
        raise ValueError("recording seed contains an active turn")
