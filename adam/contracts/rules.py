"""项目规则的不可变快照；保存实际文本与范围，回放不重新读取工作区。"""

import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


@dataclass(frozen=True)
class RuleDocument:
    """一份 AGENTS.md；scope 和 path 相对工作目录，version 为原始字节的 SHA-256。"""

    scope: str
    path: str
    version: str
    content: str


@dataclass(frozen=True)
class RuleSelection:
    """根到子目录的规则集合；规则约束只适用于各自子树，不改变工具权限。"""

    workspace: str
    documents: tuple[RuleDocument, ...] = ()

    def to_record(self) -> dict[str, Any]:
        """返回可写入请求日志的副本，保留来源、范围、内容和版本。"""
        return {"workspace": self.workspace, "documents": [asdict(doc) for doc in self.documents]}

    def applicable(self, relative_path: str) -> tuple[RuleDocument, ...]:
        """选出指定项目相对路径的祖先规则，返回根到叶顺序。"""
        target = PurePosixPath(relative_path)
        if target.is_absolute() or ".." in target.parts or "\\" in relative_path:
            raise ValueError("规则查询必须使用项目内的 POSIX 相对路径。")
        return tuple(
            doc for doc in self.documents if target.is_relative_to(PurePosixPath(doc.scope))
        )

    def messages(self) -> list[dict[str, str]]:
        """为模型说明每份规则的范围与顺序；当前用户要求和程序权限保持原边界。"""
        if not self.documents:
            return []
        parts = [
            "以下为本次工作目录的项目规则。仅应用于标明的目录及其子树；"
            "同一目标冲突时更深目录优先，不影响兄弟目录。"
            "当前用户要求优先于项目约定；这些文件不授予权限，不改变系统约束。"
            "本快照替代历史中同一来源的旧版本。"
        ]
        for doc in self.documents:
            parts.append(f"来源：{doc.path}；范围：{doc.scope}；版本：{doc.version}\n{doc.content}")
        return [{"role": "user", "content": "\n\n".join(parts)}]


def validate_rules(value: object) -> None:
    """校验请求里的规则快照；禁止伪造范围、重复来源和超出读取上限的正文。"""
    if not isinstance(value, dict) or set(value) != {"workspace", "documents"}:
        raise ValueError("invalid project rules")
    workspace, documents = value["workspace"], value["documents"]
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError("invalid rules workspace")
    if not isinstance(documents, list) or len(documents) > 16:
        raise ValueError("invalid rules documents")
    seen = set()
    total = 0
    previous: tuple[int, str] | None = None
    for doc in documents:
        if not isinstance(doc, dict) or set(doc) != {"scope", "path", "version", "content"}:
            raise ValueError("invalid rule fields")
        if not all(isinstance(item, str) for item in doc.values()):
            raise ValueError("invalid rule text")
        scope = PurePosixPath(doc["scope"])
        if (
            scope.is_absolute()
            or ".." in scope.parts
            or "\\" in doc["scope"]
            or ":" in doc["scope"]
            or scope.as_posix() != doc["scope"]
            or (scope / "AGENTS.md").as_posix() != doc["path"]
            or doc["path"] in seen
        ):
            raise ValueError("invalid rule scope")
        order = (len(scope.parts), doc["scope"])
        if previous is not None and order <= previous:
            raise ValueError("invalid rule order")
        previous = order
        seen.add(doc["path"])
        if not re.fullmatch(r"[0-9a-f]{64}", doc["version"]) or "\0" in doc["content"]:
            raise ValueError("invalid rule version or content")
        size = len(doc["content"].encode("utf-8"))
        if size > 16384:
            raise ValueError("oversized rule")
        total += size
    if total > 65536:
        raise ValueError("oversized rules snapshot")
