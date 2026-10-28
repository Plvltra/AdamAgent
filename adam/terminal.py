"""终端角色样式；颜色只用于展示，不进入模型消息或会话记录。"""

import os
import sys

from colorama import Fore, Style


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
