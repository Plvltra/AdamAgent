"""生成请求、产物与不可变作业快照；不绑定具体供应商。"""

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from math import isfinite
from typing import Literal, Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionPlan, ConsumptionSnapshot, Quantities
from adam.contracts.media import MediaRef, MediaSource

Parameter = str | int | float | bool
JobState = Literal["running", "succeeded", "failed", "cancelled", "unknown"]


@dataclass(frozen=True)
class GenerationSpec:
    """单个图片或音频产物；参数由适配器进一步校验，种子不保证跨后端一致。"""

    kind: Literal["image", "audio"]
    prompt: str
    parameters: tuple[tuple[str, Parameter], ...] = ()
    seed: int | None = None

    def __post_init__(self) -> None:
        if (
            self.kind not in {"image", "audio"}
            or not self.prompt.strip()
            or len(self.prompt) > 8000
        ):
            raise ValueError("invalid generation kind or prompt")
        if self.seed is not None and (type(self.seed) is not int or not 0 <= self.seed < 2**32):
            raise ValueError("invalid generation seed")
        if not isinstance(self.parameters, tuple) or len(self.parameters) > 32:
            raise ValueError("invalid generation parameters")
        names = set()
        for item in self.parameters:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("invalid generation parameter pair")
            name, value = item
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
                or name in names
                or type(value) not in {str, int, float, bool}
                or (isinstance(value, str) and len(value) > 2000)
                or (type(value) is float and not isfinite(value))
            ):
                raise ValueError("invalid generation parameter")
            names.add(name)


@dataclass(frozen=True)
class GeneratedMedia:
    """后端返回完整产物与实际用量；解码及生成质量由具体适配器负责验证。"""

    data: bytes
    mime_type: str
    usage: Quantities
    seed_used: int | None = None


@dataclass(frozen=True)
class GenerationJob:
    """宿主保存的完整快照；unknown不可重新派发，media为空不证明远端未生成。"""

    job_id: str
    backend: str
    spec: GenerationSpec
    source: MediaSource
    plan: ConsumptionPlan
    state: JobState
    usage: Quantities
    media: MediaRef | None = None
    seed_used: int | None = None
    cancel_requested: bool = False
    error: str | None = None


class GenerationBackend(Protocol):
    name: str
    supports_seed: bool

    def prepare(self, spec: GenerationSpec) -> ConsumptionPlan:
        """本地校验参数/凭据并冻结预留和价格；不得发起生成或其他计费请求。"""
        ...

    def generate(self, spec: GenerationSpec, *, cancel: CancellationToken) -> GeneratedMedia:
        """仅尝试一次，检查取消；远端取消不保证撤销已产生的产物或费用。"""
        ...


class GenerationBudget(Protocol):
    """由宿主注入现有UnitBudget，不让生成包反向依赖应用实现。"""

    def reserve(self, estimate: Quantities) -> AbstractContextManager[None]: ...

    def update(self, owner: str, usage: ConsumptionSnapshot) -> None: ...
