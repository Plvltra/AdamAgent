"""可重建的文档索引快照；文件锁及原子替换保护并发刷新。"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from adam.config.settings import DocumentConfig
from adam.documents.models import Chunk, DocumentError, DocumentNotice
from adam.documents.snapshots import digest, load_snapshot, save_snapshot
from adam.documents.source import DocumentSource, Source
from adam.documents.text import split_document


@dataclass(frozen=True)
class Corpus:
    """一次成功刷新的语料快照；generation 绑定范围、文件版本和分块设置。"""

    chunks: tuple[Chunk, ...]
    generation: str
    file_count: int
    notices: tuple[DocumentNotice, ...] = ()


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
        # 解析版本只使可重建索引失效，不能改变已保存知识候选所属的资料范围目录。
        self.cache_scope = {
            **self.scope,
            "source_format": "physical-pdf-pages-v1",
            "max_pdf_pages": config.max_pdf_pages,
            "max_pdf_text_chars": config.max_pdf_text_chars,
        }
        key = digest(self.cache_scope)[:24]
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
                notices: list[DocumentNotice] = []
                for source in self.source.scan():
                    self.check_cancel()
                    old = previous.get(source.path)
                    if old is not None and old["version"] == source.version:
                        items = [Chunk(**value) for value in old["chunks"]]
                    else:
                        items = _split_source(source, self.config.chunk_chars)
                    issues = _source_notices(source)
                    notices.extend(issues)
                    chunks.extend(items)
                    if len(chunks) > self.config.max_chunks:
                        raise DocumentError("文档片段数量超限，请缩小目录或增大片段长度。")
                    documents[source.path] = {
                        "version": source.version,
                        "chunks": [chunk.to_dict() for chunk in items],
                        **(
                            {"notices": [asdict(notice) for notice in issues]}
                            if source.pages is not None
                            else {}
                        ),
                    }
                payload = {"scope": self.cache_scope, "documents": documents}
                # 保持纯文本语料原有身份，不能因新增 PDF 能力使已发布知识全部变成旧版本。
                generation = digest({"scope": self.scope, "documents": documents})
                self.check_cancel()
                if documents != previous or not self.path.exists():
                    save_snapshot(self.path, payload)
                return Corpus(tuple(chunks), generation, len(documents), tuple(notices))
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
                or payload["scope"] != self.cache_scope
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
    """禁止缓存混入其他来源；PDF 页码与页内行号不能被当成普通文本全局行号。"""
    expected = {"version", "chunks"}
    if Path(path).suffix.lower() == ".pdf":
        expected.add("notices")
    if not isinstance(document, dict) or document.keys() != expected:
        raise ValueError("document schema")
    version = document["version"]
    if (
        not isinstance(version, str)
        or len(version) != 64
        or not isinstance(document["chunks"], list)
        or not isinstance(document.get("notices", []), list)
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
            or (
                Path(path).suffix.lower() == ".pdf"
                and (type(chunk.page) is not int or not 1 <= chunk.page <= 2048)
            )
            or (Path(path).suffix.lower() != ".pdf" and chunk.page is not None)
        ):
            raise ValueError("chunk metadata")
    for value in document.get("notices", []):
        notice = DocumentNotice(**value)
        if (
            notice.path != path
            or notice.version != version
            or Path(path).suffix.lower() != ".pdf"
            or type(notice.page) is not int
            or not 1 <= notice.page <= 2048
            or notice.kind not in {"image_only", "no_extractable_text", "unread_images"}
        ):
            raise ValueError("document notice")


def _split_source(source: Source, max_chars: int) -> list[Chunk]:
    """PDF 每页单独分块，防止相同文字、相同行号在不同物理页生成重复身份。"""
    if source.pages is None:
        return split_document(source.path, source.text, source.version, max_chars)
    return [
        chunk
        for page in source.pages
        for chunk in split_document(
            source.path, page.text, source.version, max_chars, page=page.page
        )
    ]


def _source_notices(source: Source) -> list[DocumentNotice]:
    """有图片或无可提取文本的页始终留下来源；文字可检索不等于读懂了整页。"""
    return [
        DocumentNotice(
            source.path,
            source.version,
            page.page,
            "unread_images" if page.status == "text" else page.status,
        )
        for page in source.pages or ()
        if page.has_images or page.status != "text"
    ]
