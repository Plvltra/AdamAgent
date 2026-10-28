"""终端角色样式与公共失败展示；颜色不进入模型消息或会话记录。"""

import os
import sys
from collections.abc import Callable

from colorama import Fore, Style

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


def show_failure(error: AgentError, write: Callable[[str], None] = print) -> None:
    """按公共契约展示失败及未保存回答；不判断底层异常类型，也不决定恢复或重试。"""
    if error.partial_reply is not None:
        write(role_prefix("assistant", unsaved=True) + error.partial_reply)
    write(error.user_message)
