"""可选终端输入泵；唯一读线程区分补充/停止与常规输入、批准回答。"""

from collections.abc import Callable
from queue import Empty, Full, Queue
from threading import Thread

from adam.application.facade import AgentApplication
from adam.contracts.errors import AgentError


class LiveInput:
    """终端生命周期内独占readline；不在每轮创建遗留的阻塞input线程。"""

    def __init__(self, readline: Callable[[], str], write: Callable[[str], None]) -> None:
        self.readline, self.write = readline, write
        self.application: AgentApplication | None = None
        self._lines: Queue[str | None] = Queue(maxsize=64)
        self._thread = Thread(target=self._pump, daemon=True)

    def start(self, application: AgentApplication) -> None:
        """应用组装后启动唯一读线程，主执行线程继续负责工具审批及会话写入。"""
        self.application = application
        self._thread.start()

    def read(self, prompt: str) -> str:
        """普通聊天和批准共用一个输入通道，补充命令不会被误当作批准答案。"""
        self.write(prompt)
        while True:
            if self.application:
                self.application.check_interruption()
            try:
                line = self._lines.get(timeout=0.1)
                break
            except Empty:
                continue
        if line is None:
            self._lines.put(None)
            raise EOFError
        return line

    def _pump(self) -> None:
        while True:
            try:
                line = self.readline()
            except (EOFError, OSError):
                line = ""
            if not line:
                self._lines.put(None)
                return
            text = line.rstrip("\r\n")
            if self.application is not None and (text.startswith("/add ") or text == "/stop"):
                try:
                    if text == "/stop":
                        self.application.stop_current()
                        self.write("已请求停止当前任务。")
                    else:
                        receipt = self.application.submit_input(text[5:].strip())
                        self.write(f"补充已接受：{receipt.sequence}")
                except AgentError as error:
                    self.write(str(error))
                continue
            try:
                self._lines.put_nowait(text)
            except Full:
                self.write("普通待输入队列已满，此行未接受；/stop仍可使用。")
