"""持久终端每次批准的不可变计划，供应用、工具和CLI共享。"""

from dataclasses import dataclass

from adam.contracts.models import CommandRequest


@dataclass(frozen=True)
class TerminalRequest:
    """command为启动计划，不推断shell当前cwd；text原样写入stdin，env为显式覆盖。"""

    action: str
    terminal_id: str
    command: CommandRequest
    env: tuple[tuple[str, str], ...] = ()
    text: str = ""
    eof: bool = False
