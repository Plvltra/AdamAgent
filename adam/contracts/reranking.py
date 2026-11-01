"""可选精排后端的最小契约；只接收候选文本并返回分数，不能新增或改写来源。"""

from typing import Protocol


class RerankError(Exception):
    """可降级的精排依赖或推理故障；取消与存储故障不属于此类。"""


class RerankBackend(Protocol):
    """离线后端需保持输入顺序，一段对应一个有限分数；不在查询时自动下载模型。"""

    @property
    def identity(self) -> str: ...

    def score(self, query: str, passages: list[str]) -> list[float]: ...
