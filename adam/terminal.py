"""终端角色、失败与上下文展示；颜色不进入模型消息或会话记录。"""

import json
import os
import sys
from collections.abc import Callable

from colorama import Fore, Style

from adam.context import RequestContext
from adam.errors import AgentError


def role_prefix(role: str, *, unsaved: bool = False) -> str:
    """返回角色标签；交互终端给前缀加色，重定向、NO_COLOR 或 dumb 终端用纯文本。

    unsaved 为助手标签附加“未保存”；其他角色保留原名。样式在标签后重置，
    避免正文及随后输入继承颜色；始终保留文字以辅助区分角色。
    """
    labels = {"user": "你", "assistant": "Adam"}
    label = labels.get(role, role)
    if role == "assistant" and unsaved:
        label += "（未保存）"
    label += ">"
    if (
        role in labels
        and sys.stdout.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    ):
        color = Fore.CYAN if role == "user" else Fore.YELLOW
        return f"{Style.BRIGHT}{color}{label}{Style.RESET_ALL} "
    return label + " "


def show_context(
    context: RequestContext, write: Callable[[str], None] = print, *, full: bool = False
) -> None:
    """按需展示基础预览及估算；full 展开消息/工具原文，不展示连接配置或认证头。"""
    estimate, limits = context.estimate, context.limits
    write("上下文> 下一次请求的基础预览；尚未包含下一条用户输入，不是最近请求快照。")
    write(
        f"模型：{context.model}；消息 {len(context.messages)} 条；工具说明 {len(context.tools)} 项。"
    )
    write(
        f"估算输入：{estimate.input_tokens} token"
        f"（系统 {estimate.system_tokens}，历史 {estimate.history_tokens}，"
        f"本轮 {estimate.turn_tokens}，工具说明 {estimate.tools_tokens}，摘要 {estimate.summary_tokens}）。"
    )
    write(
        f"历史：保留 {len(context.selected_turn_ids)} 轮，省略 {len(context.omitted_turn_ids)} 轮；原始日志保留。"
    )
    if context.pruned_tools:
        write(f"工具结果：已缩减 {len(context.pruned_tools)} 项旧结果；完整内容仍在日志。")
    if context.summary is not None:
        write(
            f"摘要：{context.summary.summary_id}；覆盖 {len(context.summary.covered_turn_ids)} 轮。"
        )
    if context.omitted_turn_ids:
        write("省略范围：" + context.omitted_turn_ids[0] + " … " + context.omitted_turn_ids[-1])
    if context.selected_turn_ids:
        write("保留范围：" + context.selected_turn_ids[0] + " … " + context.selected_turn_ids[-1])
    remaining = limits.max_input_tokens - context.budgeted_input_tokens
    availability = f"剩余 {remaining}" if remaining >= 0 else f"超出 {-remaining}"
    write(
        f"输入预算：估算 {estimate.input_tokens} + 安全余量 {limits.safety_margin_tokens}"
        f" / 上限 {limits.max_input_tokens} token；{availability} token。"
    )
    write(
        f"输出预留：{context.max_output_tokens} token（另计）；"
        f"输入、余量与输出合计约 {context.budgeted_input_tokens + context.max_output_tokens} token。"
    )
    write("以上为近似估算和应用预算，不是模型窗口容量或实际计费；真实发送前会连同新输入重新检查。")
    if full:
        write(
            json.dumps(
                {"messages": context.messages, "tools": context.tools}, ensure_ascii=False, indent=2
            )
        )


def show_failure(error: AgentError, write: Callable[[str], None] = print) -> None:
    """按公共契约展示失败及未保存回答；不判断底层异常类型，也不决定恢复或重试。"""
    if error.partial_reply is not None:
        write(role_prefix("assistant", unsaved=True) + error.partial_reply)
    write(error.user_message)
