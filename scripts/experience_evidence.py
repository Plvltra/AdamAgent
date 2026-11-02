"""B36离线经验证据账本；记录支持/反例和版本，不自动接受或写入项目记忆。"""

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from scripts.injection_capture import write_record


def create(
    directory: Path, *, lesson: str, applies_when: str, exceptions: str, environment: str
) -> dict[str, Any]:
    """创建单写者离线账本；适用条件、例外及环境版本必须明确，不覆盖既有目录。"""
    if any(not value.strip() for value in (lesson, applies_when, exceptions, environment)):
        raise ValueError("lesson, applicability, exceptions and environment required")
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "format": "adam_experience_evidence_v1",
        "revision": 0,
        "lesson": lesson,
        "applies_when": applies_when,
        "exceptions": exceptions,
        "environment": environment,
        "evidence": [],
        "validation": {"status": "unverified"},
        "accepted": False,
    }
    write_record(directory / "0000.json", record, exclusive=True)
    return record


def load(directory: Path) -> dict[str, Any]:
    """读取最新不可变版本；遇到残缺最新记录报错，不回退成看似完整的旧状态。"""
    files = sorted(directory.glob("[0-9][0-9][0-9][0-9].json"))
    if not files:
        raise ValueError("experience ledger is empty")
    value = json.loads(files[-1].read_text(encoding="utf-8"))
    if (
        value.get("format") != "adam_experience_evidence_v1"
        or value.get("revision") != len(files) - 1
    ):
        raise ValueError("invalid experience revision sequence")
    return value


def link(
    directory: Path,
    source: Path,
    *,
    task_id: str,
    stance: Literal["support", "counterexample"],
    reason: str,
    environment: str,
) -> dict[str, Any]:
    """关联显式指定的JSON轨迹及字节版本；立场由宿主给出，是论据标注而非因果证明。"""
    if stance not in {"support", "counterexample"} or not all(
        v.strip() for v in (task_id, reason, environment)
    ):
        raise ValueError("invalid evidence attribution")
    raw = source.read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("trace exceeds limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("trace must be a JSON object")
    value = load(directory)
    digest = hashlib.sha256(raw).hexdigest()
    if any(e["task_id"] == task_id or e["sha256"] == digest for e in value["evidence"]):
        raise ValueError("duplicate task or trace")
    value["evidence"].append(
        {
            "task_id": task_id,
            "source": str(source.resolve()),
            "sha256": digest,
            "stance": stance,
            "reason": reason,
            "environment": environment,
        }
    )
    value["validation"] = {"status": "unverified", "reason": "evidence_changed"}
    return save_revision(directory, value)


def inspect(directory: Path, *, environment: str) -> dict[str, Any]:
    """报告当前证据可用性和正反计数；来源变化或环境变化单列，不按票数认定经验正确。"""
    value = load(directory)
    evidence = []
    for entry in value["evidence"]:
        try:
            current = hashlib.sha256(Path(entry["source"]).read_bytes()).hexdigest()
            status = "current" if current == entry["sha256"] else "changed"
        except OSError:
            status = "missing"
        evidence.append(
            {
                **entry,
                "source_status": status,
                "environment_matches": entry["environment"] == environment,
            }
        )
    return {
        **value,
        "evidence": evidence,
        "environment_matches": value["environment"] == environment,
        "support_count": sum(e["stance"] == "support" for e in evidence),
        "counterexample_count": sum(e["stance"] == "counterexample" for e in evidence),
        "scope": "annotated evidence, not proven causal rule",
    }


def save_revision(directory: Path, value: dict[str, Any]) -> dict[str, Any]:
    """追加新版本，独占创建防止覆盖；离线单写者使用，不提供并发合并或自动重试。"""
    if value["revision"] >= 9999:
        raise ValueError("experience revision limit")
    value = {**value, "revision": value["revision"] + 1}
    write_record(directory / f"{value['revision']:04}.json", value, exclusive=True)
    return value
