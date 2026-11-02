"""B37将经验变为一个Skill的小型变更提案；仅写审查产物，不安装或运行。"""

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from adam.skills.frontmatter import header_from_bytes
from scripts.experience_evidence import load
from scripts.experience_validation import candidate_text
from scripts.injection_capture import write_record


def propose(
    ledger: Path, workspace: Path, output: Path, *, name: str, environment: str
) -> dict[str, Any]:
    """生成单个skills/<name>/SKILL.md的diff、候选文件和来源；旧文件字节只读。

    现有Skill保留原内容并追加经验段；新Skill生成最小元数据。这里只生成指导，
    不生成可执行程序；权限、评估器及内核不属于可修改目标。output不得在技能目录内。
    """
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", name):
        raise ValueError("invalid skill name")
    workspace = workspace.resolve()
    output = output.absolute()
    skills = workspace / "skills"
    target = skills / name / "SKILL.md"
    if output.resolve() != output or output.is_relative_to(skills) or target.resolve() != target:
        raise ValueError("proposal must not redirect paths or enter installed skills")
    value = load(ledger)
    draft = candidate_text(ledger, environment=environment)
    before = target.read_bytes() if target.exists() else b""
    if len(before) > 32000:
        raise ValueError("existing skill too large for minimal proposal")
    if before:
        header_from_bytes(before, name)
        text = before.decode("utf-8")
    else:
        description = "适用于" + value["applies_when"]
        text = (
            f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n"
        )
    text += "\n## 经验建议\n\n" + draft + "\n"
    after = text.encode("utf-8")
    header_from_bytes(after, name)
    if len(after) > 40000:
        raise ValueError("candidate skill too large")
    relative = f"skills/{name}/SKILL.md"
    diff = "".join(
        difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=relative if before else "/dev/null",
            tofile=relative,
        )
    )
    output.mkdir(parents=True, exist_ok=False)
    package = output / "candidate" / name
    package.mkdir(parents=True)
    (package / "SKILL.md").write_bytes(after)
    (output / "change.diff").write_text(diff, encoding="utf-8")
    ledger_path = ledger / f"{value['revision']:04}.json"
    proposal = {
        "format": "adam_skill_proposal_v1",
        "name": name,
        "target": relative,
        "workspace": str(workspace),
        "before_sha256": _digest(before) if before else None,
        "after_sha256": _digest(after),
        "before_text": before.decode("utf-8"),
        "ledger_revision": value["revision"],
        "ledger_path": str(ledger_path.resolve()),
        "ledger_sha256": _digest(ledger_path.read_bytes()),
        "environment": environment,
        "hypothesis": value["lesson"],
        "sources": value["evidence"],
        "validation": value["validation"],
        "kind": "instruction_only",
        "status": "proposed",
    }
    write_record(output / "proposal.json", proposal, exclusive=True)
    return proposal


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
