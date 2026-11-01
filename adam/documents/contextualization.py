"""独立缓存片段的短语境说明；说明只供索引增强，不能冒充源文档原文。"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock, Timeout

from adam.documents.index import Corpus
from adam.documents.models import Chunk, DocumentError
from adam.documents.snapshots import digest, load_snapshot, save_snapshot


@dataclass(frozen=True)
class ContextExcerpt:
    """送入生成器的原文前缀；行号属于整个分块，original_chars 保留截断前的字符数。"""

    chunk_id: str
    path: str
    version: str
    start_line: int
    end_line: int
    title: str
    text: str
    original_chars: int


@dataclass(frozen=True)
class ContextRequest:
    """只提供目标与同文件的紧邻片段；文本总字符数有界，缺失内容不能由生成器猜补。"""

    target: ContextExcerpt
    neighbors: tuple[ContextExcerpt, ...]
    max_prefix_chars: int


@dataclass(frozen=True)
class ContextPrefix:
    """原文身份与生成说明分别保存；input_sha256 绑定完整的有界生成输入。"""

    chunk_id: str
    path: str
    source_version: str
    input_sha256: str
    generator: str
    text: str
    prefix_sha256: str
    basis: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """返回说明与来源身份；此记录不是可直接引用的原文片段。"""
        return asdict(self)


class ContextBuilder(Protocol):
    """生成有界说明的窄接口；身份变化使缓存失效，不提供文件或工具执行能力。"""

    @property
    def identity(self) -> str: ...

    def build(self, request: ContextRequest) -> str:
        """返回不超过请求上限的非空说明；失败不自动重试。"""
        ...


class ExtractiveContextBuilder:
    """本地确定性基线，仅标注文档、标题及相邻原文，不声称解析了指代或缩写。"""

    identity = "extractive-context-v1"

    def build(self, request: ContextRequest) -> str:
        """截取明确标为相邻资料的原文，不改写或替换目标片段。"""
        target = request.target
        text = f"文档：{Path(target.path).name}；章节：{target.title or '无标题'}。"
        if request.neighbors:
            text += "相邻原文（仅供理解语境）：" + request.neighbors[0].text
        return text[: request.max_prefix_chars]


class ContextPrefixStore:
    """持锁生成或复用同版本说明；全部生成成功才原子替换缓存并返回。

    缓存仅减少重复生成，调用者必须传入已经授权、成功刷新的 Corpus。
    本阶段提供独立 API，不会自动将说明加入关键词、向量或聊天上下文。
    """

    def __init__(
        self,
        path: Path,
        *,
        max_prefix_chars: int = 300,
        max_input_chars: int = 1200,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> None:
        if (
            type(max_prefix_chars) is not int
            or not 1 <= max_prefix_chars <= 1000
            or type(max_input_chars) is not int
            or not 32 <= max_input_chars <= 8000
        ):
            raise DocumentError("语境说明上限须为 1～1000 字符，原文输入上限须为 32～8000 字符。")
        self.path = path
        self.max_prefix_chars, self.max_input_chars = max_prefix_chars, max_input_chars
        self.check_cancel = check_cancel
        self.lock = FileLock(str(path.with_suffix(".lock")), timeout=5)

    def prepare(
        self, corpus: Corpus, builder: ContextBuilder | None = None
    ) -> tuple[ContextPrefix, ...]:
        """返回与当前每个分块对应的说明；源、生成器或窗口变化时重建，删除的源不返回旧说明。

        同文件前后最多各一个邻块；正文总长度受 max_input_chars 限制。
        损坏缓存、非法说明或保存失败会拒绝整批结果，旧文件保留；取消不降级成成功。
        """
        builder = builder or ExtractiveContextBuilder()
        identity = builder.identity
        if not isinstance(identity, str) or not identity.strip() or len(identity) > 300:
            raise DocumentError("语境生成器必须提供有界的稳定身份。")
        chunks = corpus.chunks
        if len(chunks) > 10000 or len({c.chunk_id for c in chunks}) != len(chunks):
            raise DocumentError("语境输入片段过多或身份重复。")
        self.check_cancel()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                previous = self._load()
                notes = []
                for chunk, neighbors in _neighbors(chunks):
                    self.check_cancel()
                    request = self._request(chunk, neighbors)
                    input_hash = digest(asdict(request))
                    old = previous.get(chunk.chunk_id)
                    if old and (old.input_sha256, old.generator) == (input_hash, identity):
                        note = old
                    else:
                        text = builder.build(request)
                        self.check_cancel()
                        if (
                            not isinstance(text, str)
                            or not text.strip()
                            or len(text) > self.max_prefix_chars
                        ):
                            raise DocumentError(
                                "语境生成器未返回有效的有界说明；没有保存部分结果。"
                            )
                        note = ContextPrefix(
                            chunk.chunk_id,
                            chunk.path,
                            chunk.version,
                            input_hash,
                            identity,
                            text,
                            digest(text),
                            tuple(n.chunk_id for n in request.neighbors),
                        )
                    # 即使缓存结构合法，也只能复用当前来源的说明，不接受错绑的源身份。
                    if (note.path, note.source_version, note.basis) != (
                        chunk.path,
                        chunk.version,
                        tuple(n.chunk_id for n in request.neighbors),
                    ):
                        raise DocumentError("语境说明与当前原文身份不一致。")
                    notes.append(note)
                self.check_cancel()
                if {n.chunk_id: n for n in notes} != previous or not self.path.exists():
                    save_snapshot(self.path, {"notes": [n.to_dict() for n in notes]})
                return tuple(notes)
        except Timeout as error:
            raise DocumentError("语境说明缓存正在生成，请稍后重试。") from error
        except OSError as error:
            raise DocumentError("语境说明读写失败，未返回部分结果；旧缓存保留。") from error

    def _request(self, chunk: Chunk, neighbors: tuple[Chunk, ...]) -> ContextRequest:
        """先留一半正文预算给目标，其余按前后邻块分配；记录截断前长度供生成器判断。"""
        target_size = min(len(chunk.text), self.max_input_chars // 2)
        remaining = self.max_input_chars - target_size
        excerpts = []
        for i, neighbor in enumerate(neighbors):
            size = min(len(neighbor.text), remaining // (len(neighbors) - i))
            if size:
                excerpts.append(_excerpt(neighbor, size))
                remaining -= size
        return ContextRequest(_excerpt(chunk, target_size), tuple(excerpts), self.max_prefix_chars)

    def _load(self) -> dict[str, ContextPrefix]:
        """校验缓存字段与说明校验和；损坏明确报错，不静默重建以掩盖来源问题。"""
        payload = load_snapshot(self.path)
        if payload is None:
            return {}
        try:
            if (
                payload.keys() != {"notes"}
                or not isinstance(payload["notes"], list)
                or len(payload["notes"]) > 10000
            ):
                raise ValueError("context schema")
            notes = {}
            for value in payload["notes"]:
                if not isinstance(value, dict) or not isinstance(value.get("basis"), list):
                    raise ValueError("context note")
                note = ContextPrefix(**{**value, "basis": tuple(value["basis"])})
                if (
                    any(
                        not isinstance(s, str) or not s
                        for s in (
                            note.chunk_id,
                            note.path,
                            note.source_version,
                            note.input_sha256,
                            note.generator,
                            note.text,
                            note.prefix_sha256,
                        )
                    )
                    or len(note.text) > 1000
                    or len(note.generator) > 300
                    or len(note.chunk_id) != 16
                    or len(note.source_version) != 64
                    or len(note.input_sha256) != 64
                    or note.prefix_sha256 != digest(note.text)
                    or len(note.basis) > 2
                    or any(not isinstance(s, str) or len(s) != 16 for s in note.basis)
                    or note.chunk_id in notes
                ):
                    raise ValueError("context identity")
                notes[note.chunk_id] = note
            return notes
        except (TypeError, ValueError, KeyError) as error:
            raise DocumentError("语境说明缓存损坏或版本不支持；保留文件，请移走后重建。") from error


def _neighbors(chunks: tuple[Chunk, ...]) -> list[tuple[Chunk, tuple[Chunk, ...]]]:
    """保留索引分块顺序，避免同一行的长段被按哈希乱序；邻块必须属于同文件同版本。"""
    documents: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        documents.setdefault(chunk.path, []).append(chunk)
    result = []
    for parts in documents.values():
        if len({chunk.version for chunk in parts}) != 1:
            raise DocumentError("同一文档包含多个版本，不能生成语境说明。")
        for i, chunk in enumerate(parts):
            result.append((chunk, tuple(parts[max(0, i - 1) : i] + parts[i + 1 : i + 2])))
    return result


def _excerpt(chunk: Chunk, size: int) -> ContextExcerpt:
    return ContextExcerpt(
        chunk.chunk_id,
        chunk.path,
        chunk.version,
        chunk.start_line,
        chunk.end_line,
        chunk.title,
        chunk.text[:size],
        len(chunk.text),
    )
