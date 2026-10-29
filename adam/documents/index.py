"""可重建的文档索引快照；文件锁及原子替换保护并发刷新。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.config.settings import DocumentConfig
from adam.documents.models import Chunk, DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from adam.documents.source import DocumentSource
from adam.documents.text import split_document


@dataclass(frozen=True)
class Corpus:
    """一次成功刷新的语料快照；generation 绑定范围、文件版本和分块设置。"""

    chunks: tuple[Chunk, ...]
    generation: str
    file_count: int


class DocumentIndex:
    """缓存只负责减少重复分块；搜索仍以本次成功读取的源文件为准。"""

    def __init__(
        self,
        cache_dir: Path,
        workspace: Path,
        config: DocumentConfig,
        authorize: Callable[[str], Path],
        check_cancel: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.check_cancel = check_cancel or (lambda: None)
        self.source = DocumentSource(workspace, config, authorize, cache_dir, self.check_cancel)
        self.scope = {
            "workspace": str(workspace.resolve()),
            "roots": sorted(map(str, self.source.roots)),
            "chunk_chars": config.chunk_chars,
        }
        key = digest(self.scope)[:24]
        self.path = cache_dir / (key + ".json")
        self.lock = FileLock(str(cache_dir / (key + ".lock")), timeout=5)

    def refresh(self) -> Corpus:
        """持锁重读、刷新新增/修改/删除并返回快照；任何失败都不返回旧资料。

        只有变化后才落盘。损坏缓存明确报错并保留原文件；删除对应缓存可重建，
        会话日志不受影响。不同工作目录和语料配置使用独立缓存文件。
        """
        self.check_cancel()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                self.check_cancel()
                previous = self._load()
                documents: dict[str, Any] = {}
                chunks: list[Chunk] = []
                for source in self.source.scan():
                    self.check_cancel()
                    old = previous.get(source.path)
                    if old is not None and old["version"] == source.version:
                        items = [Chunk(**value) for value in old["chunks"]]
                    else:
                        items = split_document(
                            source.path, source.text, source.version, self.config.chunk_chars
                        )
                    chunks.extend(items)
                    if len(chunks) > self.config.max_chunks:
                        raise DocumentError("文档片段数量超限，请缩小目录或增大片段长度。")
                    documents[source.path] = {
                        "version": source.version,
                        "chunks": [chunk.to_dict() for chunk in items],
                    }
                payload = {"scope": self.scope, "documents": documents}
                generation = digest(payload)
                self.check_cancel()
                if documents != previous or not self.path.exists():
                    save_snapshot(self.path, payload)
                return Corpus(tuple(chunks), generation, len(documents))
        except Timeout as error:
            raise DocumentError("文档索引正在被其他进程使用，请稍后查询。") from error
        except OSError as error:
            raise DocumentError("文档索引读写失败，未返回旧资料；此前有效索引保留。") from error

    def _load(self) -> dict[str, Any]:
        """验证范围及片段来源；容器版本、校验和与原子写入由 snapshots 共用。"""
        payload = load_snapshot(self.path)
        if payload is None:
            return {}
        try:
            if (
                payload.keys() != {"scope", "documents"}
                or payload["scope"] != self.scope
                or not isinstance(payload["documents"], dict)
            ):
                raise ValueError("index scope")
            for path, document in payload["documents"].items():
                _validate_document(path, document)
            return payload["documents"]
        except (ValueError, TypeError, KeyError) as error:
            raise DocumentError(
                "文档索引损坏或版本不支持；保留原文件，请移走该缓存后重建。"
            ) from error


def _validate_document(path: str, document: object) -> None:
    """禁止缓存中混入其他来源、非法行号或错误内容类型。"""
    if not isinstance(document, dict) or document.keys() != {"version", "chunks"}:
        raise ValueError("document schema")
    version = document["version"]
    if (
        not isinstance(version, str)
        or len(version) != 64
        or not isinstance(document["chunks"], list)
    ):
        raise ValueError("document version")
    for value in document["chunks"]:
        if not isinstance(value, dict):
            raise ValueError("chunk schema")
        chunk = Chunk(**value)
        if (
            chunk.path != path
            or chunk.version != version
            or any(
                not isinstance(v, str)
                for v in (chunk.chunk_id, chunk.path, chunk.title, chunk.text)
            )
            or type(chunk.start_line) is not int
            or type(chunk.end_line) is not int
            or not 1 <= chunk.start_line <= chunk.end_line
            or not chunk.text
        ):
            raise ValueError("chunk metadata")
