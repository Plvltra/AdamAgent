"""委派数据和流程接口；工具层不依赖具体 Conversation 或线程调度实现。"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ChildRun:
    """一次派发的来源与结果；child_turn_id 连接子会话的真实请求和工具轨迹。"""

    run_id: str
    parent_turn_id: str
    request_id: str
    tool_call_id: str
    task: str
    model: str
    status: str = "running"
    child_turn_id: str | None = None
    reply: str = ""
    error: str = ""


@dataclass
class ChildAgent:
    """可续聊的子会话；启动时固定工作目录和读取范围，runs 保留历次委派。"""

    agent_id: str
    name: str
    workspace: str
    read_dirs: list[str]
    runs: list[ChildRun] = field(default_factory=list)


class RequestAccount(Protocol):
    """一次会话访问共享额度的接口；在拥有该会话日志的线程中调用。"""

    def check(self) -> None:
        """刷新自己的已结算消费，并检查整组额度。"""
        ...

    def request(self, estimate: int) -> AbstractContextManager[None]:
        """预留一次请求的估算消费，退出前从日志结算；超限时不登记远端请求。"""
        ...

    def usage(self) -> tuple[int, bool]:
        """返回整组消费；正常在途请求不成为未知消费。"""
        ...


class Delegation(Protocol):
    """主轮次的委派边界；进入、提供约束、收集完成检查和退出清理。"""

    def begin(self, session_id: str, turn_id: str, model: str) -> None:
        """绑定主会话并开始本轮新建计数。"""
        ...

    def instructions(self) -> str:
        """返回程序约束与状态提示，不把子 Agent 的回复提升为系统指令。"""
        ...

    def accept_final(self) -> bool:
        """只有结果已回传给主模型时才接受最终回答。"""
        ...

    def end(self) -> None:
        """取消并等待本组未完成工作；不删除子会话。"""
        ...

    def close(self) -> None:
        """释放调度器拥有的资源；外部存储和模型仍由组装入口关闭。"""
        ...


class DelegationTools(Protocol):
    """主 Agent 可调用的协作操作；实现负责身份、并发、来源和持久化。"""

    def spawn(self, name: str, task: str) -> dict[str, Any]: ...
    def send(self, agent_id: str, task: str) -> dict[str, Any]: ...
    def list_agents(self) -> dict[str, Any]: ...
    def wait(self, agent_ids: list[str], timeout_seconds: int) -> dict[str, Any]: ...
    def interrupt(self, agent_id: str) -> dict[str, Any]: ...
