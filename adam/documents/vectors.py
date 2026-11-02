"""小语料向量缓存和余弦检索；不需要独立向量数据库服务。"""

import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from filelock import FileLock, Timeout

from adam.documents.embedding import EmbeddingBackend, unit_vector
from adam.documents.models import Chunk, DocumentError, SearchHit
from adam.documents.snapshots import digest, load_snapshot, save_snapshot


class VectorSearch:
    """按片段及索引前缀复用向量；源版本或模型变化会重建，删除的片段不参与检索。"""

    def __init__(
        self,
        path: Path,
        backend: EmbeddingBackend,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> None:
        self.path, self.backend, self.check_cancel = path, backend, check_cancel
        self.lock = FileLock(str(path.with_suffix(".lock")), timeout=5)

    def search(
        self,
        chunks: Sequence[Chunk],
        query: str,
        limit: int,
        minimum: float,
        *,
        prefixes: Mapping[str, str] | None = None,
    ) -> list[SearchHit]:
        """刷新向量后返回达到余弦阈值的候选；阈值不保证证据充分。

        可选语境前缀只用于编码，缓存键同时绑定前缀；命中仍返回原始分块。
        """
        self.check_cancel()
        if not chunks:
            return []
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                self.check_cancel()
                identity = self.backend.identity
                previous = self._load(identity)
                vectors: dict[str, list[float]] = {}
                keys: dict[str, str] = {}
                for chunk in chunks:
                    self.check_cancel()
                    prefix = (prefixes or {}).get(chunk.chunk_id, "")
                    key = digest([chunk.chunk_id, prefix])[:16] if prefix else chunk.chunk_id
                    keys[chunk.chunk_id] = key
                    if key in previous:
                        vectors[key] = previous[key]
                    else:
                        text = chunk.title + "\n" + chunk.text
                        rows = self.backend.passages([prefix + "\n" + text if prefix else text])
                        if len(rows) != 1:
                            raise ValueError("passage count")
                        vectors[key] = unit_vector(rows[0])
                self.check_cancel()
                needle = unit_vector(self.backend.query(query))
                if any(len(row) != len(needle) for row in vectors.values()):
                    raise ValueError("query dimension")
                self.check_cancel()
                if vectors != previous:
                    save_snapshot(self.path, {"backend": identity, "vectors": vectors})
            hits = [
                SearchHit(
                    chunk,
                    sum(a * b for a, b in zip(needle, vectors[keys[chunk.chunk_id]], strict=True)),
                )
                for chunk in chunks
            ]
            return sorted(
                (hit for hit in hits if hit.score >= minimum),
                key=lambda hit: (
                    -hit.score,
                    hit.chunk.path,
                    hit.chunk.page or 0,
                    hit.chunk.start_line,
                ),
            )[:limit]
        except Timeout as error:
            raise DocumentError("文档向量缓存正在刷新，请稍后查询。") from error
        except OSError as error:
            raise DocumentError("文档向量缓存读写失败；旧缓存保留，未返回旧资料。") from error
        except (ValueError, TypeError, KeyError) as error:
            raise DocumentError("文档向量格式无效或维度不一致；未返回检索结果。") from error

    def _load(self, identity: str) -> dict[str, list[float]]:
        """验证缓存内部结构；模型版本不同是正常失效，格式损坏仍明确报错。"""
        value = load_snapshot(self.path)
        if value is None:
            return {}
        if (
            value.keys() != {"backend", "vectors"}
            or not isinstance(value["backend"], str)
            or not isinstance(value["vectors"], dict)
        ):
            raise ValueError("vector cache schema")
        vectors = {}
        for key, row in value["vectors"].items():
            if not isinstance(key, str) or len(key) != 16:
                raise ValueError("index text identity")
            unit_vector(row)
            if not math.isclose(math.hypot(*row), 1, rel_tol=1e-6):
                raise ValueError("non-normalized cached vector")
            vectors[key] = row
        return vectors if value["backend"] == identity else {}
