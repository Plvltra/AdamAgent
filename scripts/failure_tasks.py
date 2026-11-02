"""B35从可复现的教学失败生成任务；来源明确为本地构造，不冒充真实模型历史。"""

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Blueprint:
    """宿主持有的任务蓝图；expected_files/reply不写入模型工作目录。"""

    task_id: str
    family: str
    prompt: str
    files: dict[str, str]
    expected_files: dict[str, str]
    expected_reply: dict[str, str] | None
    source_sha256: str = ""


def variant(family: str, index: int, source_sha256: str = "") -> Blueprint:
    """给固定失败机制生成有不同文件/字段的同类变体；不执行模型生成代码。"""
    if type(index) is not int or not 0 <= index <= 20:
        raise ValueError("invalid variant index")
    name = f"sample_{index}.py"
    correct = "def add(a, b):\n    return a + b\n"
    if family == "operator_repair":
        files = {name: correct.replace("a + b", "a - b"), "keep.txt": "unchanged\n"}
        expected = {**files, name: correct}
        prompt = f"把{name}中add的减法改成加法，其余内容保持不变。"
        reply = None
    elif family == "preserve_correct":
        files = {name: "def difference(a, b):\n    return a - b\n"}
        expected = dict(files)
        prompt = f"检查{name}的差值实现，正确则不修改。"
        reply = None
    elif family == "source_lookup":
        value = ("杭州", "南京", "武汉")[index % 3]
        files = {f"release_{index}.txt": f"发布地点：{value}\n"}
        expected = dict(files)
        prompt = (
            f"读取release_{index}.txt，只返回JSON包含city和source（来源文件名），不要修改文件。"
        )
        reply = {"city": value, "source": f"release_{index}.txt"}
    else:
        raise ValueError("unknown failure family")
    return Blueprint(f"{family}_{index}", family, prompt, files, expected, reply, source_sha256)


def prepare(blueprint: Blueprint, workspace: Path) -> None:
    """只向全新目录写任务输入；判据及来源摘要留在宿主。"""
    workspace.mkdir(parents=True, exist_ok=False)
    for name, content in blueprint.files.items():
        if Path(name).name != name or ":" in name or name in {".", ".."}:
            raise ValueError("invalid fixture name")
        (workspace / name).write_text(content, encoding="utf-8")


def verify(blueprint: Blueprint, workspace: Path, reply: str) -> dict[str, Any]:
    """以实际文件及固定真值判定，不信任回答自述；额外文件、链接或未知产物均失败。"""
    reasons = []
    try:
        actual = {}
        for path in workspace.iterdir():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 100000:
                raise ValueError("invalid output file")
            actual[path.name] = path.read_text(encoding="utf-8")
        if actual != blueprint.expected_files:
            reasons.append("file_state_mismatch")
        if blueprint.expected_reply is not None and json.loads(reply) != blueprint.expected_reply:
            reasons.append("answer_or_source_mismatch")
    except (OSError, ValueError, UnicodeError):
        reasons.append("unverifiable_output")
    return {"passed": not reasons, "reasons": reasons}


def generate(root: Path) -> dict[str, Any]:
    """实际复现三条失败后保存来源和两种变体；不读取用户历史、模型配置或敏感文件。"""
    root.mkdir(parents=True, exist_ok=False)
    blueprints, sources = [], []
    for family in ("operator_repair", "preserve_correct", "source_lookup"):
        original = variant(family, 0)
        workspace = root / family / "reproduction"
        prepare(original, workspace)
        reply, action = "已经完成", "finish_without_edit"
        if family == "preserve_correct":
            (workspace / "sample_0.py").write_text("def difference(a, b):\n    return a + b\n")
            action = "incorrectly_replace_subtraction"
        elif family == "source_lookup":
            reply = '{"city":"未知","source":"invented.txt"}'
            action = "answer_without_reading"
        verdict = verify(original, workspace, reply)
        if verdict["passed"]:
            raise ValueError("failure reproduction unexpectedly passed")
        trace = {
            "origin": "constructed_local_failure",
            "family": family,
            "input": original.prompt,
            "steps": [{"action": action, "reply": reply}],
            "initial_files": original.files,
            "actual_files": {p.name: p.read_text(encoding="utf-8") for p in workspace.iterdir()},
            "verification": verdict,
        }
        raw = json.dumps(trace, ensure_ascii=False, sort_keys=True).encode("utf-8")
        source = root / family / "source.json"
        source.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        sources.append(
            {"family": family, "path": source.relative_to(root).as_posix(), "sha256": digest}
        )
        blueprints.extend(asdict(variant(family, n, digest)) for n in (1, 2))
    result = {
        "format": "adam_failure_tasks_v1",
        "sources": sources,
        "blueprints": blueprints,
        "scope": "constructed failures; not model-generated or training data",
    }
    (root / "dataset.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
