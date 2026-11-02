"""有限导航仿真接原工具循环；环境由宿主注入，不提供通用机器人执行命令。"""

from collections.abc import Callable
from functools import partial
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.models import ExecutionRecorder
from adam.robotics.grid import GridWorld
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry

SPECS = {
    "nav_observe": schema(
        "观察二维仿真实际位置/障碍/目标/版本；x向右y向下，非真实机器人。", {}, []
    ),
    "nav_move": schema(
        "仿真移动一格；dx/dy必须一个为0另一个±1，传最近version。撞墙也耗步数。",
        {
            "version": ("string", "最近观察版本"),
            "dx": ("integer", "横向增量"),
            "dy": ("integer", "纵向增量"),
        },
        ["version", "dx", "dy"],
    ),
    "nav_stop": schema("急停当前仿真，不可由工具复位；已到达/耗尽状态保留。", {}, []),
}


class NavigationTools:
    """一个注入环境只绑定首次使用的主会话，换会话不能继续控制它。"""

    def __init__(self, world: GridWorld, session: Callable[[], str | None]) -> None:
        self.world, self.session = world, session
        self.owner: str | None = None

    def direct(
        self, name: str, args: dict[str, Any], record: ExecutionRecorder | None
    ) -> dict[str, Any]:
        """普通注册表入口仍检查会话；聊天权限和日志继续由原执行器处理。"""
        return self.invoke(name, args, record, CancellationToken())

    def invoke(
        self,
        name: str,
        args: dict[str, Any],
        record: ExecutionRecorder | None,
        cancel: CancellationToken,
    ) -> dict[str, Any]:
        """返回实际环境转移；取消不移动，急停不依赖模型提供的旧版本。"""
        sid = self.session()
        if sid is None or (self.owner is not None and self.owner != sid):
            raise ToolError("navigation environment belongs to another or missing session")
        self.owner = sid
        cancel.check()
        if name == "nav_observe":
            return self.world.observe()
        if name == "nav_stop":
            return self.world.stop()
        return self.world.move(args["version"], args["dx"], args["dy"], cancel)


def register_navigation(
    registry: ToolRegistry, world: GridWorld, session: Callable[[], str | None]
) -> ToolRegistry:
    """仅显式注入的内存仿真；移动需act模式，不连接任何硬件，不给子任务继承。"""
    tools = NavigationTools(world, session)
    return registry.extended(
        SPECS,
        {name: partial(tools.direct, name) for name in SPECS},
        capabilities={
            "nav_observe": ToolCapability.SESSION_STATE,
            "nav_move": ToolCapability.EXTERNAL_ACTION,
            "nav_stop": ToolCapability.SESSION_STATE,
        },
        interruptible_handlers={name: partial(tools.invoke, name) for name in SPECS},
        groups=dict.fromkeys(SPECS, "navigation"),
    )
