"""B37双集对照与人工发布；只允许提案中的单个Skill文件。"""

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from adam.config.settings import AppConfig
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
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if not gate.get("eligible") or gate["proposal_sha256"] != _sha(
        (proposal_dir / "proposal.json").read_bytes()
    ):
        raise ValueError("evaluation does not authorize this proposal")
    if gate["candidate_sha256"] != proposal["after_sha256"] or any(
        _sha(Path(r["path"]).read_bytes()) != r["sha256"] for r in gate["reports"]
    ):
        raise ValueError("evaluation artifacts changed")
    if (_sha(target.read_bytes()) if target.exists() else None) != proposal["before_sha256"]:
        raise ValueError("target version conflict")
    accepted = approve(
        {
            "proposal": proposal,
            "evaluation": gate,
            "diff": (proposal_dir / "change.diff").read_text(encoding="utf-8"),
        }
    )
    _event(proposal_dir, "approved" if accepted else "rejected")
    if not accepted:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(candidate.read_bytes())
    _event(proposal_dir, "installed")
    return True


def rollback(proposal_dir: Path, approve: Callable[[dict[str, Any]], bool]) -> bool:
    """仅回滚仍等于候选版本的目标；外部修改冲突时不覆盖，需人工确认。"""
    proposal, _, target = _read(proposal_dir)
    if not target.exists() or _sha(target.read_bytes()) != proposal["after_sha256"]:
        raise ValueError("rollback version conflict")
    if not approve(proposal):
        _event(proposal_dir, "rollback_rejected")
        return False
    if proposal["before_sha256"] is None:
        target.unlink()
    else:
        target.write_bytes(proposal["before_text"].encode("utf-8"))
    _event(proposal_dir, "rolled_back")
    return True


def _read(directory: Path) -> tuple[dict[str, Any], Path, Path]:
    """限制发布位置并核对候选/原始快照；不接受提案任意路径。"""
    proposal = json.loads((directory / "proposal.json").read_text(encoding="utf-8"))
    name = proposal["name"]
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name)
        or proposal["target"] != f"skills/{name}/SKILL.md"
    ):
        raise ValueError("invalid skill target")
    candidate = directory / "candidate" / name / "SKILL.md"
    target = Path(proposal["workspace"]) / proposal["target"]
    if target.resolve() != target.absolute() or candidate.resolve() != candidate.absolute():
        raise ValueError("release path redirects")
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
