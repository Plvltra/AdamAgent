"""B35任务来源校验、按族隔离与B31运行器适配。"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.failure_tasks import Blueprint, prepare, variant, verify
from scripts.task_eval_assets import PreparedTask, TaskCase, TaskProvider

FAMILY_SPLITS = {
    "operator_repair": "derive",
    "preserve_correct": "derive",
    "source_lookup": "holdout",
}


def load_provider(dataset_path: Path, split: str) -> TaskProvider:
    """验证来源字节与已知蓝图后选择整个任务族；同族所有变体只属于一个集合。"""
    if split not in {"derive", "holdout"}:
        raise ValueError("invalid dataset split")
    raw = dataset_path.read_bytes()
    data = json.loads(raw)
    if data.get("format") != "adam_failure_tasks_v1":
        raise ValueError("invalid dataset format")
    sources = {}
    for source in data["sources"]:
        family = source["family"]
        if (
            family not in FAMILY_SPLITS
            or family in sources
            or source["path"] != f"{family}/source.json"
        ):
            raise ValueError("invalid source family")
        path = dataset_path.parent / source["path"]
        if path.resolve() != path.absolute():
            raise ValueError("source redirects through link")
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("source changed")
        sources[family] = source["sha256"]
    if set(sources) != set(FAMILY_SPLITS):
        raise ValueError("missing source family")
    selected: dict[str, Blueprint] = {}
    seen = set()
    for row in data["blueprints"]:
        blueprint = Blueprint(**row)
        if blueprint.family not in sources or blueprint.task_id in seen:
            raise ValueError("invalid or duplicate blueprint")
        seen.add(blueprint.task_id)
        index = int(blueprint.task_id.rsplit("_", 1)[1])
        if row != asdict(variant(blueprint.family, index, sources[blueprint.family])):
            raise ValueError("blueprint no longer matches known task generator")
        if FAMILY_SPLITS[blueprint.family] == split:
            selected[blueprint.task_id] = blueprint
    if not selected:
        raise ValueError("empty dataset split")

    def prepare_case(root: Path, case_id: str) -> PreparedTask:
        """每次新建独立环境；来源和验证器都留在workspace之外。"""
        blueprint = selected[case_id]
        root.mkdir(parents=True, exist_ok=False)
        workspace = root / "workspace"
        prepare(blueprint, workspace)
        baseline = {
            name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
            for name in blueprint.files
        }
        return PreparedTask(
            TaskCase(case_id, blueprint.family, blueprint.prompt, {}),
            root,
            workspace,
            root / "sessions",
            root / "memories",
            baseline,
            blueprint.expected_reply or {},
        )

    def check(task: PreparedTask, reply: str, tools: tuple[str, ...]) -> dict[str, Any]:
        """复用实际文件终态验证器，工具自述不充当结果。"""
        return verify(selected[task.case.case_id], task.workspace, reply)

    return TaskProvider(
        tuple(selected),
        prepare_case,
        check,
        {
            "suite": "B35_generated",
            "split": split,
            "dataset_sha256": hashlib.sha256(raw).hexdigest(),
            "family_splits": FAMILY_SPLITS,
            "sources": sources,
        },
    )
