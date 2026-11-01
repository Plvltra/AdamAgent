"""文档与记忆共用的向量接口和数值边界；不加载模型或访问磁盘。"""

import math
from typing import Protocol


class EmbeddingBackend(Protocol):
    """可替换的向量生成接口；测试可替代推理，但不伪称真实模型效果。"""

    @property
    def identity(self) -> str:
        """标识模型及编码方式，变化后必须重新生成文档或记忆向量。"""
        ...

    def passages(self, texts: list[str]) -> list[list[float]]:
        """每篇输入返回一个有限、非零的向量。"""
        ...

    def query(self, text: str) -> list[float]:
        """返回与 passages 位于同一向量空间的查询向量。"""
        ...


def unit_vector(value: object) -> list[float]:
    """拒绝非法维度、布尔值、NaN 和零向量，返回余弦比较使用的单位向量。"""
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 8192
        or any(type(number) not in {int, float} or not math.isfinite(number) for number in value)
    ):
        raise ValueError("invalid embedding")
    norm = math.hypot(*(float(number) for number in value))
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("zero or overflowing embedding")
    return [float(number) / norm for number in value]
