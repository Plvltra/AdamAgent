"""B31固定任务资产与独立终态检查；不执行模型生成代码，不请求模型。"""

import ast
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adam.session.storage import SessionStore
from scripts.memory_eval import seed_memories
from scripts.reflection_eval import TASKS


@dataclass(frozen=True)
class TaskCase:
    """固定任务身份与要求；预期值由宿主验证器持有，不作为模型提示。"""

    case_id: str
    category: str
    prompt: str
    expected: dict[str, Any]


CASES = (
    TaskCase("repair", "code", "把train.py中add的减法运算改成加法，其他文件保持不变。", {}),
    TaskCase(
        "counterexample",
        "code",
        "difference.py实现差值，若已经正确不要修改。只返回JSON：changed为布尔值。",
        {"changed": False},
    ),
    TaskCase(
        "memory-basic",
        "memory",
        "@wang-dev 喜欢喝什么？依据项目记忆，只返回JSON：drink用tea或coffee。",
        {"drink": "tea"},
    ),
    TaskCase(
        "memory-cross-session",
        "memory",
        "查证当前项目保存的“发布地点”和“发布日期”会话，给出发布地点和日期。只返回JSON：city、date、source_session_ids（两个来源会话ID）。",
        {},
    ),
    TaskCase(
        "memory-association",
        "memory",
        "我刚完成本项目代码修改，交付前还有什么既定要求？依据既有项目约定，只返回JSON：checks是检查项目数组，用unit_tests表示单元测试。",
        {"checks": ["unit_tests"]},
    ),
    TaskCase(
        "no-tools",
        "counterexample",
        TASKS["irrelevant"] + " 本题只判断是否需要工具，只返回JSON：needs_tools为布尔值。",
        {"needs_tools": False},
    ),
)

INITIAL_FILES = {
    "train.py": "def add(a: int, b: int) -> int:\n    return a - b\n",
    "difference.py": "def difference(a: int, b: int) -> int:\n    return a - b\n",
}
REPAIRED = "def add(a: int, b: int) -> int:\n    return a + b\n"


@dataclass(frozen=True)
class PreparedTask:
    """一次独立试验；baseline和expected留在宿主，workspace不包含答案或验证器。"""

    case: TaskCase
    root: Path
    workspace: Path
    sessions: Path
    memories: Path
    baseline: dict[str, str]
    expected: dict[str, Any]


def prepare_task(root: Path, case_id: str) -> PreparedTask:
    """创建全新目录作为环境重置；已存在即拒绝，不删除或复用上次产物。

    复用B06的真实记忆资产，并创建两个独立JSONL会话供来源查证。
    """
    case = next((item for item in CASES if item.case_id == case_id), None)
    if case is None:
        raise ValueError("unknown task case")
    root = root.absolute()
    if root.resolve() != root:
        raise ValueError("evaluation root cannot redirect through a link")
    root.mkdir(parents=True, exist_ok=False)
    _, roots, _ = seed_memories(root)
    workspace = roots["alpha"]
    for name, content in INITIAL_FILES.items():
        (workspace / name).write_text(content, encoding="utf-8")
    sessions = root / "sessions"
    with SessionStore(sessions) as store:
        ids = []
        for title, question, answer in (
            ("发布地点", "这次在哪里发布？", "本次发布地点是杭州。"),
            ("发布日期", "这次什么时候发布？", "本次发布日期是2026-11-15。"),
        ):
            identity = store.create_session(title, "固定评估历史，只记录已给定事实。")
            store.append_turn(identity, question, answer)
            ids.append(identity)
    expected = dict(case.expected)
    if case_id == "memory-cross-session":
        expected = {"city": "杭州", "date": "2026-11-15", "source_session_ids": sorted(ids)}
    baseline = _files(workspace)
    (root / "fixture.json").write_text(
        json.dumps(
            {
                "format": "adam_task_fixture_v1",
                "case_id": case_id,
                "baseline": baseline,
                "seed_sessions": ids,
                "source_assets": [
                    "scripts.memory_eval.seed_memories",
                    "scripts.reflection_eval.TASKS",
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return PreparedTask(case, root, workspace, sessions, root / "memories", baseline, expected)


def verify_task(task: PreparedTask, reply: str, tools: tuple[str, ...] = ()) -> dict[str, Any]:
    """独立检查文件终态与精确JSON字段；失败和缺少证据都不算成功。

    这是固定练习题的确定性验收，不宣称可评分任意自然语言或通用代码正确性。
    """
    reasons = []
    try:
        current = _files(task.workspace)
        changed = {
            name
            for name in current.keys() | task.baseline.keys()
            if current.get(name) != task.baseline.get(name)
        }
        if task.case.case_id == "repair":
            if changed != {"train.py"}:
                reasons.append("修改文件集合不符合任务要求")
            actual = ast.parse((task.workspace / "train.py").read_text(encoding="utf-8"))
            if ast.dump(actual) != ast.dump(ast.parse(REPAIRED)):
                reasons.append("add终态未满足指定加法实现")
        else:
            if changed:
                reasons.append("不应修改任务文件")
            value = json.loads(reply)
            if isinstance(value, dict) and task.case.case_id == "memory-cross-session":
                sources = value.get("source_session_ids")
                if isinstance(sources, list) and all(isinstance(item, str) for item in sources):
                    value["source_session_ids"] = sorted(sources)
            if value != task.expected:
                reasons.append("结构化答案或来源不符合固定真值")
        if task.case.case_id == "no-tools" and tools:
            reasons.append("普通问题不应调用工具")
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        reasons.append(f"无法验证终态：{type(error).__name__}")
    return {"case_id": task.case.case_id, "passed": not reasons, "reasons": reasons}


def _files(workspace: Path) -> dict[str, str]:
    """只检查任务目录，不跟随链接；有界读取防止产物撑满评估内存。"""
    result = {}
    for path in workspace.rglob("*"):
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError("task output contains a link")
        if path.is_file():
            if len(result) >= 100 or path.stat().st_size > 1_000_000:
                raise ValueError("task output exceeds evaluation limit")
            result[path.relative_to(workspace).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


@dataclass(frozen=True)
class TaskProvider:
    """宿主注入的固定任务集及来源；验证器不暴露给模型工作区。"""

    cases: tuple[str, ...]
    prepare: Callable[[Path, str], PreparedTask]
    verify: Callable[[PreparedTask, str, tuple[str, ...]], dict[str, Any]]
    provenance: dict[str, Any]
