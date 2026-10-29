"""本地 embedding 适配器；正常检索只读取已下载模型，不上传文档。"""

import importlib
import importlib.metadata
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, cast

from adam.documents.models import DocumentError


class EmbeddingBackend(Protocol):
    """可替换的向量生成接口；测试可替代推理，但不伪称真实模型效果。"""

    @property
    def identity(self) -> str:
        """标识模型及编码方式，变化后必须重新生成文档向量。"""
        ...

    def passages(self, texts: list[str]) -> list[list[float]]:
        """每篇输入返回一个有限、非零的向量。"""
        ...

    def query(self, text: str) -> list[float]:
        """返回与 passages 位于同一向量空间的查询向量。"""
        ...


class ArrayVector(Protocol):
    """第三方数组的最小转换契约，不要求关键词模式安装 numpy。"""

    def tolist(self) -> list[float]: ...


class TextEncoder(Protocol):
    """只暴露本适配器使用的两种编码方法。"""

    def passage_embed(self, texts: list[str]) -> Iterable[ArrayVector]: ...

    def query_embed(self, texts: list[str]) -> Iterable[ArrayVector]: ...


class FastEmbedBackend:
    """在 CPU 上运行 FastEmbed；初始化延迟到首次需要语义检索时。"""

    def __init__(self, cache_dir: Path, model_name: str, *, allow_download: bool = False) -> None:
        """allow_download 只由用户显式执行的模型准备命令开启。"""
        self.cache_dir, self.model_name = cache_dir, model_name
        self.allow_download = allow_download
        self._model: TextEncoder | None = None

    @property
    def identity(self) -> str:
        """固定依赖版本、模型名及窗口合并算法，避免复用不兼容的向量。"""
        try:
            version = importlib.metadata.version("fastembed")
        except importlib.metadata.PackageNotFoundError as error:
            raise DocumentError(
                "语义检索需要可选依赖，请运行 uv sync --extra documents。"
            ) from error
        return f"fastembed:{version}:{self.model_name}:window256-mean-v1"

    def passages(self, texts: list[str]) -> list[list[float]]:
        """分窗口推理后合并，避免长片段被模型静默截断。"""
        return [self._encode(text, query=False) for text in texts]

    def query(self, text: str) -> list[float]:
        """使用模型的 query_embed，保留模型要求的查询编码差异。"""
        return self._encode(text, query=True)

    def prepare(self) -> None:
        """加载并验证模型；仅显式下载模式允许缺失时从模型源获取文件。"""
        self._load()

    def _load(self) -> TextEncoder:
        """返回缓存的模型实例；第三方初始化失败转成可见的文档错误。"""
        if self._model is not None:
            return self._model
        _ = self.identity
        try:
            module = importlib.import_module("fastembed")
            model = module.TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self.cache_dir),
                threads=1,
                providers=["CPUExecutionProvider"],
                local_files_only=not self.allow_download,
            )
            self._model = cast(TextEncoder, model)
        except Exception as error:
            # 第三方下载、分词器及 ONNX 异常不属于模型对话错误，也不能伪装成无匹配。
            raise DocumentError(
                "embedding 模型无法加载；请安装可选依赖并显式准备模型。检索不会自动联网下载。"
            ) from error
        return self._model

    def _encode(self, text: str, *, query: bool) -> list[float]:
        """每个窗口最多 256 字符，均值汇总后归一化；不以截断内容冒充完整片段。"""
        model = self._load()
        windows = [text[i : i + 256] for i in range(0, len(text), 256)] or [text]
        try:
            method = model.query_embed if query else model.passage_embed
            vectors = [unit_vector(row.tolist()) for row in method(windows)]
            if len(vectors) != len(windows) or len({len(row) for row in vectors}) != 1:
                raise ValueError("embedding dimensions")
            return unit_vector([sum(values) for values in zip(*vectors, strict=True)])
        except Exception as error:
            raise DocumentError("embedding 推理失败；没有返回检索结果，请检查本地模型。") from error


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
