"""纯文本分块与中英文关键词提取，不访问文件系统或模型。"""

import hashlib
import re

from adam.documents.models import Chunk


def terms(text: str) -> list[str]:
    """英文按词与标识符、中文按相邻双字切分；不进行语义改写。"""
    words = re.findall(r"[a-z0-9_]+", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return words


def split_document(path: str, text: str, version: str, max_chars: int = 1000) -> list[Chunk]:
    """按标题、段落和长度产生有界片段；超长单行拆开但保留真实行号。

    path 是可定位的来源路径；片段 ID 同时绑定文件版本、位置和文本。
    空白行不单独索引。标题保存在元数据中，不伪造成其他片段的原文。
    """
    if type(max_chars) is not int or max_chars < 32:
        raise ValueError("max_chars must be at least 32")
    result: list[Chunk] = []
    title = ""
    parts: list[str] = []
    first = last = size = 0

    def flush() -> None:
        nonlocal parts, size
        body = "\n".join(parts).strip()
        if body:
            key = f"{path}\0{version}\0{first}:{last}\0{body}"
            identifier = hashlib.sha256(key.encode()).hexdigest()[:16]
            result.append(Chunk(identifier, path, title, first, last, body, version))
        parts, size = [], 0

    for number, line in enumerate(text.splitlines(), 1):
        if re.match(r"^#{1,6}\s+", line):
            flush()
            title = line.lstrip("#").strip()
        if not line.strip():
            flush()
            continue
        for offset in range(0, len(line), max_chars):
            fragment = line[offset : offset + max_chars]
            if parts and size + len(fragment) + 1 > max_chars:
                flush()
            if not parts:
                first = number
            last = number
            parts.append(fragment)
            size += len(fragment) + 1
    flush()
    return result
