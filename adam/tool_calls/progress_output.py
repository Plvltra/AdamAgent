"""把程序的重复失败观察加入工具回传；缩短预览时保留实际状态与宿主附件引用。"""

import json
from typing import Any

from adam.contracts.errors import StorageError
from adam.tool_calls.progress import ProgressAction, ProgressDecision


def encode_progress(output: str, decision: ProgressDecision, max_chars: int) -> str:
    """添加有界 no_progress 字段，返回最终落日志且发送给模型的同一份 JSON。

    output 已经经过原工具/附件编码器；完整附件不改写。只在真实业务失败时附加观察，
    零计数直接返回。扩展不能以工具自带的同名字段控制结论，调用方须先移除该保留字段。
    """
    if not decision.consecutive_failures:
        return output
    observation: dict[str, Any] = {
        "action": decision.action.value,
        "repeats": decision.consecutive_failures,
        "fingerprint": decision.fingerprint,
    }
    if decision.action is ProgressAction.WARN:
        observation["hint"] = "相同调用连续失败；请检查原因并换策略，继续相同失败将停止本轮。"
    elif decision.action is ProgressAction.STOP:
        observation["hint"] = "达到连续相同失败上限，本轮已停止；保留已有动作，不自动重试。"
    data = json.loads(output)
    data["no_progress"] = observation
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded) <= max_chars:
        return encoded

    # 新观察也占结果额度；只缩短显示，不把附件完整性或命令退出码改成成功。
    preview: dict[str, Any] = {
        "ok": data.get("ok") is True,
        "truncated": True,
        "no_progress": observation,
    }
    for key in ("status", "exit_code", "artifact", "artifact_unavailable"):
        if key in data:
            preview[key] = data[key]
    preview["preview"] = output
    while True:
        encoded = json.dumps(preview, ensure_ascii=False)
        if len(encoded) <= max_chars:
            return encoded
        if not preview["preview"]:
            raise StorageError("工具结果上限不足以容纳执行状态、附件引用与停止观察。")
        preview["preview"] = preview["preview"][: len(preview["preview"]) // 2]
