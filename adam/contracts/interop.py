"""远端委派批准和宿主费用上限，不把本地批准当作远端机器权限。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteAgentPolicy:
    """宿主指定回环服务及每任务含查询/取消的约定总价上限；不是费用实测。"""

    origin: str
    max_tasks: int
    task_cost_ceiling_microusd: int
    max_cost_microusd: int
    cost_source: str

    def __post_init__(self) -> None:
        if (
            type(self.max_tasks) is not int
            or not 1 <= self.max_tasks <= 100
            or type(self.task_cost_ceiling_microusd) is not int
            or self.task_cost_ceiling_microusd < 0
            or type(self.max_cost_microusd) is not int
            or self.max_cost_microusd < 0
            or not self.cost_source
            or len(self.cost_source) > 256
        ):
            raise ValueError("invalid remote task budget")


@dataclass(frozen=True)
class RemoteApproval:
    """准确的源、文本或任务与约定上限；本地凭据及工具权限不会传出。"""

    session_id: str
    origin: str
    operation: str
    local_id: str
    text: str | None
    remote_id: str | None
    cost_ceiling_microusd: int
    cost_source: str
