"""把程序的重复失败观察加入工具回传；缩短预览时保留实际状态与宿主附件引用。"""

import json
from typing import Any

from adam.contracts.errors import StorageError, ToolError
from adam.tool_calls.progress import ProgressAction, ProgressDecision
from adam.tool_calls.result_preview import encode_head_tail


def encode_progress(
    output: str,
    decision: ProgressDecision,
    max_chars: int,
    *,
    original: dict[str, Any] | None = None,
) -> str:
    """添加有界 no_progress 字段，返回最终落日志且发送给模型的同一份 JSON。

    output 已经经过原工具/附件编码器；完整附件不改写。只在真实业务失败时附加观察，
    零计数直接返回。扩展不能以工具自带的同名字段控制结论，调用方须先移除该保留字段。
    original 仅提供宿主原结果的文本，避免再次裁剪已带省略标记的预览；状态与引用仍取 output。
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
    for key in (
        "status",
        "exit_code",
        "environment",
        "reason",
        "policy",
        "artifact",
        "artifact_unavailable",
        "source_truncated",
    ):
        if key in data:
            preview[key] = data[key]
    source = original if original is not None else data
    texts = {
        key: source[key] for key in ("stdout", "stderr") if isinstance(source.get(key), str)
    } or {"preview": json.dumps(original, ensure_ascii=False) if original is not None else output}
    try:
        return encode_head_tail(preview, texts, max_chars, optional=("policy",))
    except ToolError:
        # 极小额度优先保留执行环境、停止原因、反馈和可读引用；正文全部省略也明确计数。
        # 不缩短版本摘要或失败指纹，原正文与完整元数据仍在附件和命令事实日志中。
        compact = {key: value for key, value in preview.items() if key != "policy"}
        reference = compact.get("artifact")
        if isinstance(reference, dict):
            compact["artifact"] = {
                key: value
                for key, value in reference.items()
                if key not in {"saved_chars", "format"}
            }
        compact["preview_omitted_chars"] = {key: len(value) for key, value in texts.items()}
        try:
            return encode_head_tail(compact, {}, max_chars)
        except ToolError as error:
            raise StorageError("工具结果上限不足以容纳执行状态、附件引用与停止观察。") from error
