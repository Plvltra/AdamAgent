"""终端增量回答的展示状态；不参与消息保存或成功判定。"""

from collections.abc import Callable

from adam.cli.terminal import role_prefix
from adam.contracts.models import ModelOutput


class ReplyOutput:
    """为每轮维护正文和换行；没有增量时仍显示非流式的完整回答。"""

    def __init__(self, write: Callable[[str], None], fragment: Callable[[str], None]) -> None:
        self.write = write
        self.fragment = fragment
        self.shown = False
        self.line_open = False

    def receive(self, event: ModelOutput) -> None:
        """显示正文片段，响应结束时补换行，方便后续工具提示独占一行。"""
        if event.kind == "response_end":
            self.close_line()
        elif event.text:
            if not self.line_open:
                self.fragment(role_prefix("assistant"))
                self.line_open = True
            self.fragment(event.text)
            self.shown = True

    def complete(self, reply: str) -> None:
        """成功时结束当前行；已流式展示过的回答不重复打印。"""
        self.close_line()
        if not self.shown:
            self.write(role_prefix("assistant") + reply)

    def incomplete(self) -> None:
        """结束部分输出并明确它不是已提交的有效回答。"""
        self.close_line()
        if self.shown:
            self.write("本轮未完成；以上部分输出未加入有效历史。")

    def close_line(self) -> None:
        """在已有正文之后补一个换行，不产生空白回答。"""
        if self.line_open:
            self.fragment("\n")
            self.line_open = False


def write_fragment(text: str) -> None:
    """立即刷新终端中的正文片段，不自动补换行。"""
    print(text, end="", flush=True)
