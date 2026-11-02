"""B36在未参与提炼的固定任务上检验经验；验证结果不自动进入长期记忆。"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.experience_evidence import inspect, load, save_revision
from scripts.injection_capture import write_record
from scripts.task_eval_assets import PreparedTask, TaskProvider


def validate(
    directory: Path,
    provider: TaskProvider,
    output: Path,
    execute: Callable[[PreparedTask, str], str],
    *,
    environment: str,
) -> dict[str, Any]:
    """将经验作为建议交给显式执行器，再用宿主独立判据检验实际终态。

    仅接收标为holdout的固定任务集；拒绝重复任务、提炼来源和变化的环境/证据。
    execute由宿主负责权限和额度，此入口不会自行调用API。异常记录为未知验证，
    不把执行器故障当作经验反例；每次要求全新output，旧验证和原始版本保留。
    """
    value = load(directory)
    observed = inspect(directory, environment=environment)
    if not observed["environment_matches"] or any(
        e["source_status"] != "current" or not e["environment_matches"]
        for e in observed["evidence"]
    ):
        raise ValueError("experience environment or source changed")
    provenance = provider.provenance
    if provenance.get("split") != "holdout" or not provider.cases:
        raise ValueError("independent holdout required")
    used = {e["task_id"] for e in value["evidence"]}
    if used & set(provider.cases):
        raise ValueError("task already used to derive experience")
    sources = provenance.get("sources", {})
    families = provenance.get("family_splits", {})
    held_sources = {
        digest for family, digest in sources.items() if families.get(family) == "holdout"
    }
    if held_sources & {e["sha256"] for e in value["evidence"]}:
        raise ValueError("holdout source already used to derive experience")
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for case_id in provider.cases:
        error = None
        try:
            task = provider.prepare(output / case_id, case_id)
            reply = execute(task, value["lesson"])
            verdict = provider.verify(task, reply, ())
            if type(verdict.get("passed")) is not bool:
                raise ValueError("invalid independent verdict")
        except Exception as failure:
            error = type(failure).__name__
            verdict = {"passed": None, "reasons": ["execution_or_verifier_error"]}
        rows.append({"case_id": case_id, "verification": verdict, "error": error})
    status = (
        "counterexample"
        if any(r["verification"]["passed"] is False for r in rows)
        else ("unverified" if any(r["error"] for r in rows) else "supported_on_holdout")
    )
    report = {
        "status": status,
        "environment": environment,
        "experience_revision": value["revision"],
        "task_provenance": provenance,
        "rows": rows,
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "observed task outcomes, not causal proof",
    }
    path = output / "validation.json"
    write_record(path, report, exclusive=True)
    value["validation"] = {
        "status": status,
        "report": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "environment": environment,
        "checked_at": report["checked_at"],
    }
    return save_revision(directory, value)
