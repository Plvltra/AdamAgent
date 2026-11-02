"""B37双集对照与人工发布；只允许提案中的单个Skill文件。"""

import difflib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from adam.config.settings import AppConfig
from adam.skills.frontmatter import header_from_bytes
from scripts.experience_evidence import inspect, load
from scripts.failure_suite import load_provider
from scripts.injection_capture import write_record
from scripts.task_eval import run_suite
from scripts.task_eval_assets import PreparedTask
from scripts.task_eval_report import compare


def evaluate(
    proposal_dir: Path,
    dataset: Path,
    output: Path,
    config: AppConfig,
    *,
    live: bool = False,
    transport_factory: Callable[[PreparedTask, str, int], httpx.MockTransport] | None = None,
) -> dict[str, Any]:
    """复用B31在改善/保留集跑基线和候选；至少一项改善、两集无退步才可提交人工判断。"""
    proposal, candidate, _ = _read(proposal_dir)
    output.mkdir(parents=True, exist_ok=False)
    candidates = {"candidate": candidate}
    if proposal["before_sha256"] is not None:
        baseline = output / "baseline_skill" / proposal["name"] / "SKILL.md"
        baseline.parent.mkdir(parents=True)
        baseline.write_text(proposal["before_text"], encoding="utf-8", newline="")
        candidates["baseline"] = baseline
    checks, reports = {}, []
    for split in ("derive", "holdout"):
        suite = run_suite(
            output / split,
            {"baseline": config, "candidate": config},
            task_provider=load_provider(dataset, split),
            skill_candidates=candidates,
            live=live,
            transport_factory=transport_factory,
        )
        paired = compare(output / split, baseline="baseline")["comparisons"][0]["pairs"]
        checks[split] = {"pairs": paired, "unknown_usage": suite["unknown_usage"]}
        for path in sorted((output / split).glob("*.json")):
            reports.append({"path": str(path.resolve()), "sha256": _sha(path.read_bytes())})
    eligible = all(
        not c["unknown_usage"] and not c["pairs"]["regressed"] and not c["pairs"]["unavailable"]
        for c in checks.values()
    )
    eligible = eligible and bool(checks["derive"]["pairs"]["improved"])
    gate = {
        "eligible": eligible,
        "mode": "live_model" if live else "mock_http",
        "proposal_sha256": _sha((proposal_dir / "proposal.json").read_bytes()),
        "candidate_sha256": proposal["after_sha256"],
        "checks": checks,
        "reports": reports,
        "scope": "fixed tasks only; human decision required",
    }
    write_record(output / "gate.json", gate, exclusive=True)
    return gate


def decide(proposal_dir: Path, gate_path: Path, approve: Callable[[dict[str, Any]], bool]) -> bool:
    """核对双集产物及目标版本后请求宿主人工决定；拒绝留记录，通过才写单个Skill。"""
    proposal, candidate, target = _read(proposal_dir)
    gate = _check_gate(proposal_dir, gate_path, proposal)
    _fresh(proposal)
    _version(target, proposal["before_sha256"])
    reviewed = _sha((proposal_dir / "proposal.json").read_bytes())
    accepted = approve(
        {
            "proposal": proposal,
            "evaluation": gate,
            "diff": "".join(
                difflib.unified_diff(
                    proposal["before_text"].splitlines(keepends=True),
                    candidate.read_text(encoding="utf-8").splitlines(keepends=True),
                    fromfile=proposal["target"],
                    tofile=proposal["target"],
                )
            ),
        }
    )
    accepted = accepted is True
    _event(proposal_dir, "approved" if accepted else "rejected")
    if not accepted:
        return False
    proposal, candidate, target = _read(proposal_dir)
    if _sha((proposal_dir / "proposal.json").read_bytes()) != reviewed:
        raise ValueError("proposal changed during approval")
    _check_gate(proposal_dir, gate_path, proposal)
    _fresh(proposal)
    _version(target, proposal["before_sha256"])
    target.parent.mkdir(parents=True, exist_ok=True)
    _replace(target, candidate.read_bytes())
    _event(proposal_dir, "installed")
    return True


def rollback(proposal_dir: Path, approve: Callable[[dict[str, Any]], bool]) -> bool:
    """仅回滚仍等于候选版本的目标；外部修改冲突时不覆盖，需人工确认。"""
    proposal, _, target = _read(proposal_dir)
    if not target.exists() or _sha(target.read_bytes()) != proposal["after_sha256"]:
        raise ValueError("rollback version conflict")
    if approve(dict(proposal)) is not True:
        _event(proposal_dir, "rollback_rejected")
        return False
    current, _, current_target = _read(proposal_dir)
    if current != proposal or current_target != target:
        raise ValueError("proposal changed during rollback")
    _version(target, proposal["after_sha256"])
    if proposal["before_sha256"] is None:
        target.unlink()
    else:
        _replace(target, proposal["before_text"].encode("utf-8"))
    _event(proposal_dir, "rolled_back")
    return True


def _read(directory: Path) -> tuple[dict[str, Any], Path, Path]:
    """限制发布位置并核对候选/原始快照；不接受提案任意路径。"""
    proposal = json.loads((directory / "proposal.json").read_text(encoding="utf-8"))
    name = proposal["name"]
    if (
        proposal.get("format") != "adam_skill_proposal_v1"
        or proposal.get("kind") != "instruction_only"
        or not Path(proposal["workspace"]).is_absolute()
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name)
        or proposal["target"] != f"skills/{name}/SKILL.md"
    ):
        raise ValueError("invalid skill target")
    candidate = directory / "candidate" / name / "SKILL.md"
    target = Path(proposal["workspace"]) / proposal["target"]
    if target.resolve() != target.absolute() or candidate.resolve() != candidate.absolute():
        raise ValueError("release path redirects")
    header_from_bytes(candidate.read_bytes(), name)
    if _sha(candidate.read_bytes()) != proposal["after_sha256"]:
        raise ValueError("candidate changed")
    if (
        proposal["before_sha256"] is not None
        and _sha(proposal["before_text"].encode("utf-8")) != proposal["before_sha256"]
    ):
        raise ValueError("original snapshot changed")
    return proposal, candidate, target


def _event(directory: Path, action: str) -> None:
    """独占追加本地发布历史，保留拒绝与回滚，不自动重试副作用。"""
    history = directory / "history"
    history.mkdir(exist_ok=True)
    write_record(
        history / f"{len(list(history.glob('*.json'))):04}.json", {"action": action}, exclusive=True
    )


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fresh(proposal: dict[str, Any]) -> None:
    """批准前后复核最新经验版本与来源，阻止过期提案安装；回滚不依赖经验仍有效。"""
    ledger_path = Path(proposal["ledger_path"])
    current = load(ledger_path.parent)
    if (
        current["revision"] != proposal["ledger_revision"]
        or _sha(ledger_path.read_bytes()) != proposal["ledger_sha256"]
    ):
        raise ValueError("experience version changed")
    observed = inspect(ledger_path.parent, environment=proposal["environment"])
    if not observed["environment_matches"] or any(
        e["source_status"] != "current" or not e["environment_matches"]
        for e in observed["evidence"]
    ):
        raise ValueError("experience source or environment changed")


def _check_gate(directory: Path, gate_path: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    """核对双集检查及产物字节，缺失/退步不能以单个eligible字段绕过。"""
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if gate.get("eligible") is not True or gate.get("proposal_sha256") != _sha(
        (directory / "proposal.json").read_bytes()
    ):
        raise ValueError("evaluation does not authorize this proposal")
    checks = gate.get("checks", {})
    if (
        set(checks) != {"derive", "holdout"}
        or not checks["derive"]["pairs"]["improved"]
        or any(
            c["unknown_usage"] or c["pairs"]["regressed"] or c["pairs"]["unavailable"]
            for c in checks.values()
        )
    ):
        raise ValueError("dual evaluation missing or regressed")
    if (
        not gate.get("reports")
        or gate["candidate_sha256"] != proposal["after_sha256"]
        or any(_sha(Path(r["path"]).read_bytes()) != r["sha256"] for r in gate["reports"])
    ):
        raise ValueError("evaluation artifacts changed")
    return gate


def _version(target: Path, expected: str | None) -> None:
    if (_sha(target.read_bytes()) if target.exists() else None) != expected:
        raise ValueError("target version conflict")


def _replace(target: Path, raw: bytes) -> None:
    """同目录临时文件刷盘后原子替换；替换前失败保留旧文件，单写者不自动重试。"""
    fd, name = tempfile.mkstemp(prefix=".adam-skill-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
