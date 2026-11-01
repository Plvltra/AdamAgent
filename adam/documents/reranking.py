"""有界本地 cross-encoder 精排；失败保留原融合次序及全部来源，不返回半次排序。"""

import importlib
import importlib.metadata
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from itertools import islice
from numbers import Real
from pathlib import Path
from typing import Protocol, cast

from adam.contracts.reranking import RerankBackend, RerankError
from adam.documents.models import SearchHit


class CrossEncoder(Protocol):
    """第三方仅负责匹配分数，外层统一限制输入、检查输出和保留引用。"""

    def rerank(
        self, query: str, documents: Iterable[str], batch_size: int = 1
    ) -> Iterable[float]: ...


class FastEmbedReranker:
    """延迟加载本地 CPU 模型；只有显式准备命令允许下载公开模型。"""

    def __init__(self, cache_dir: Path, model_name: str, *, allow_download: bool = False) -> None:
        self.cache_dir, self.model_name = cache_dir, model_name
        self.allow_download = allow_download
        self._model: CrossEncoder | None = None

    @property
    def identity(self) -> str:
        """绑定库版本与模型名；前缀窗口另由每次查询报告，不将分数当置信度。"""
        try:
            version = importlib.metadata.version("fastembed")
        except importlib.metadata.PackageNotFoundError as error:
            raise RerankError("missing_dependency") from error
        return f"fastembed:{version}:{self.model_name}"

    def prepare(self) -> None:
        """加载本地模型；allow_download 仅由宿主显式准备入口开启。"""
        self._load()

    def score(self, query: str, passages: list[str]) -> list[float]:
        """按输入顺序读取有限数量分数；第三方故障不泄露路径，也不伪造成精排成功。"""
        model = self._load()
        try:
            rows = list(islice(model.rerank(query, passages, batch_size=1), len(passages) + 1))
            if any(isinstance(value, bool) or not isinstance(value, Real) for value in rows):
                raise ValueError("non-numeric score")
            return [float(value) for value in rows]
        except Exception as error:
            raise RerankError("inference_failed") from error

    def _load(self) -> CrossEncoder:
        """只通过 FastEmbed 本地模式加载 ONNX；第三方加载异常变为明确的精排故障。"""
        if self._model is not None:
            return self._model
        _ = self.identity
        try:
            module = importlib.import_module("fastembed.rerank.cross_encoder")
            self._model = cast(
                CrossEncoder,
                module.TextCrossEncoder(
                    model_name=self.model_name,
                    cache_dir=str(self.cache_dir),
                    threads=1,
                    providers=["CPUExecutionProvider"],
                    local_files_only=not self.allow_download,
                ),
            )
        except Exception as error:
            raise RerankError("model_unavailable") from error
        return self._model


@dataclass(frozen=True)
class RerankReport:
    """区分实际精排与降级；有界前缀只影响打分，返回引用仍是原始片段。"""

    status: str
    backend: str | None
    candidates: int
    query_chars: int
    passage_chars: int
    query_truncated: bool
    truncated_passages: int
    failure: str | None = None
    input_policy: str = "prefix_then_model_tokenizer"


def rerank_hits(
    hits: list[SearchHit],
    query: str,
    backend: RerankBackend,
    *,
    query_chars: int,
    passage_chars: int,
    check_cancel: Callable[[], None],
) -> tuple[list[SearchHit], RerankReport]:
    """只重排现有候选，全部打分成功才采用；取消继续传播，已得部分分数不会返回。

    每段推理前后检查取消。输入取明确字符前缀，模型分词器仍可能进一步截断；
    这只代表对提供窗口的相关性评分，不能声称模型完整阅读了原片段。
    """
    if (
        type(query_chars) is not int
        or not 1 <= query_chars <= 1000
        or type(passage_chars) is not int
        or not 1 <= passage_chars <= 4000
        or len(hits) > 20
    ):
        raise ValueError("invalid reranking bounds")
    texts = [hit.chunk.title + "\n" + hit.chunk.text for hit in hits]
    report = RerankReport(
        "empty" if not hits else "applied",
        None,
        len(hits),
        query_chars,
        passage_chars,
        len(query) > query_chars,
        sum(len(text) > passage_chars for text in texts),
    )
    check_cancel()
    if not hits:
        return hits, report
    try:
        identity = backend.identity
        if not isinstance(identity, str) or not identity or len(identity) > 512:
            raise RerankError("invalid_backend")
        report = replace(report, backend=identity)
        scores = []
        for text in texts:
            check_cancel()
            values = backend.score(query[:query_chars], [text[:passage_chars]])
            check_cancel()
            if (
                not isinstance(values, list)
                or len(values) != 1
                or type(values[0]) not in {int, float}
                or not math.isfinite(values[0])
            ):
                raise RerankError("invalid_scores")
            scores.append(float(values[0]))
    except RerankError as error:
        check_cancel()
        reason = str(error)
        if reason not in {
            "missing_dependency",
            "model_unavailable",
            "inference_failed",
            "invalid_backend",
            "invalid_scores",
        }:
            reason = "backend_failure"
        return hits, replace(report, status="fallback", failure=reason)
    ordered = sorted(range(len(hits)), key=lambda position: (-scores[position], position))
    return [replace(hits[p], rerank_score=scores[p]) for p in ordered], report
