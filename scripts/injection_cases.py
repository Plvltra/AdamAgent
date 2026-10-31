"""B02 固定成对样本：只加载并校验数据，不请求模型、运行工具或判断抗注入效果。"""

import argparse
import json
import re
from dataclasses import dataclass, fields
from hashlib import sha256
from pathlib import Path
from typing import Any

SOURCES = frozenset({"direct", "file", "tool_result", "memory", "skill"})
BOUNDARIES = frozenset({"path", "credentials", "mode", "approval"})
EFFECTS = frozenset(
    {
        "read:public",
        "read:target",
        "read:private",
        "read:credential",
        "read:source",
        "write:target",
        "write:private",
        "run:marker",
    }
)
VARIABLES = frozenset(
    {"PUBLIC_FILE", "PRIVATE_FILE", "CREDENTIAL_FILE", "TARGET_FILE", "SOURCE_FILE"}
)
MAX_SUITE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Variant:
    """一次独立任务；mode/approval 来自评估宿主，payload 中的授权声明不能修改它们。"""

    prompt: str
    payload: str
    mode: str
    approval: str
    allowed: tuple[str, ...]
    required: tuple[str, ...]
    forbidden: tuple[str, ...]
    answer_contains: tuple[str, ...]


@dataclass(frozen=True)
class CasePair:
    """同一入口的攻击与正常任务；效果指向合成夹具目标，不携带可执行代码。"""

    case_id: str
    source: str
    boundary: str
    description: str
    attack: Variant
    benign: Variant


@dataclass(frozen=True)
class InjectionSuite:
    """保留样本修订号及源文件摘要，便于之后报告对齐同一批输入。"""

    suite_id: str
    revision: int
    pairs: tuple[CasePair, ...]
    source_sha256: str


def load_suite(path: Path) -> InjectionSuite:
    """读取最多 1 MiB 的 v1 样本；重复键、未知字段、冲突判据等抛 ValueError。

    只读取给定文件，不物化夹具、不展开真实路径或运行其中的文字。
    I/O 错误保持原异常；返回记录和内部集合不可变。
    """
    with path.open("rb") as stream:
        raw = stream.read(MAX_SUITE_BYTES + 1)
    if len(raw) > MAX_SUITE_BYTES:
        raise ValueError("样本文件超过 1 MiB。")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("样本必须是有限层级的 UTF-8 JSON。") from error
    data = _object(data, {"schema_version", "suite_id", "revision", "pairs"})
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("不支持的样本格式版本。")
    identity = _identifier(data["suite_id"])
    revision = data["revision"]
    if type(revision) is not int or not 1 <= revision <= 1_000_000:
        raise ValueError("样本修订号必须是正整数。")
    items = data["pairs"]
    if not isinstance(items, list) or not 1 <= len(items) <= 128:
        raise ValueError("样本必须包含 1～128 对任务。")
    pairs = tuple(_pair(item) for item in items)
    if len({item.case_id for item in pairs}) != len(pairs):
        raise ValueError("样本 ID 重复。")
    return InjectionSuite(identity, revision, pairs, sha256(raw).hexdigest())


def _pair(data: object) -> CasePair:
    """两侧共用入口与边界；正常侧必须要求至少一种实际效果，防止全拒绝也通过。"""
    data = _object(data, {field.name for field in fields(CasePair)})
    source, boundary = data["source"], data["boundary"]
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("未知注入入口。")
    if not isinstance(boundary, str) or boundary not in BOUNDARIES:
        raise ValueError("未知权限边界。")
    attack, benign = _variant(data["attack"], source), _variant(data["benign"], source)
    if not benign.required:
        raise ValueError("正常样本必须定义实际完成条件。")
    if attack == benign:
        raise ValueError("攻击与正常样本不能完全相同。")
    return CasePair(
        _identifier(data["case_id"]),
        source,
        boundary,
        _text(data["description"]),
        attack,
        benign,
    )


def _variant(data: object, source: str) -> Variant:
    """效果只使用固定枚举；这份契约没有任意 Python、shell 或路径执行入口。"""
    data = _object(data, {field.name for field in fields(Variant)})
    prompt, payload = _text(data["prompt"]), _text(data["payload"], allow_empty=True)
    if (source == "direct" and payload) or (source != "direct" and not payload.strip()):
        raise ValueError("间接入口需要 payload；直接输入把内容写在 prompt。")
    mode, approval = data["mode"], data["approval"]
    if mode not in ("plan", "act") or approval not in ("deny", "allow"):
        raise ValueError("执行模式或宿主批准策略无效。")
    allowed, required, forbidden = (
        _effects(data[key]) for key in ("allowed", "required", "forbidden")
    )
    if not forbidden or not set(required) <= set(allowed) or set(allowed) & set(forbidden):
        raise ValueError("允许、必需和禁止的效果矛盾或缺少禁止项。")
    stateful = {effect for effect in allowed if effect.startswith(("write:", "run:"))}
    if stateful and (mode != "act" or approval != "allow"):
        raise ValueError("允许写入或命令必须由宿主选择 act 并逐次批准。")
    answers = _texts(data["answer_contains"])
    return Variant(prompt, payload, mode, approval, allowed, required, forbidden, answers)


def _object(data: object, names: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or data.keys() != names:
        raise ValueError("样本对象字段缺失或包含未知字段。")
    return data


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("样本 ID 必须是有限长度的英文小写标识。")
    return value


def _text(value: object, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 12000
        or "\0" in value
        or (not allow_empty and not value.strip())
    ):
        raise ValueError("样本文本为空、过长或含 NUL。")
    if set(re.findall(r"\$\{([^}]+)\}", value)) - VARIABLES:
        raise ValueError("样本引用了未知夹具变量。")
    return value


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError("文本条件必须是最多 16 项的列表。")
    result = tuple(_text(item) for item in value)
    if len(set(result)) != len(result):
        raise ValueError("条件重复。")
    return result


def _effects(value: object) -> tuple[str, ...]:
    result = _texts(value)
    if set(result) - EFFECTS:
        raise ValueError("未知实际效果。")
    return result


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in items:
        if key in data:
            raise ValueError("JSON 对象存在重复键。")
        data[key] = value
    return data


def main(argv: list[str] | None = None) -> int:
    """只读展示版本和成对任务清单；不产生成功率或假冒评估报告。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    args = parser.parse_args(argv)
    suite = load_suite(args.cases)
    print(
        json.dumps(
            {
                "suite_id": suite.suite_id,
                "revision": suite.revision,
                "source_sha256": suite.source_sha256,
                "state": "not_run",
                "pairs": [
                    {"id": pair.case_id, "source": pair.source, "boundary": pair.boundary}
                    for pair in suite.pairs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
