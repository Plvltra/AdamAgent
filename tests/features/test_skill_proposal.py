"""B37.1：候选可被真实Skill加载器解析，原目标与记忆保持不动。"""

from pathlib import Path

from adam.skills.catalog import SkillCatalog
from scripts.experience_evidence import create, load
from scripts.skill_proposal import propose


def test_new_and_existing_skill_proposals_preserve_target_and_provenance(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = tmp_path / "lesson"
    create(
        ledger,
        lesson="读取文件后给出来源",
        applies_when="查证项目文件",
        exceptions="无关问题",
        environment="v1",
    )
    output = tmp_path / "proposal"
    proposal = propose(ledger, workspace, output, name="source-check", environment="v1")
    assert not (workspace / "skills").exists()
    catalog = SkillCatalog(workspace, lambda p: Path(p), root=output / "candidate")
    assert catalog.summaries()[0]["id"] == "source-check"
    assert "读取文件后给出来源" in (output / "candidate/source-check/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert proposal["before_sha256"] is None and proposal["kind"] == "instruction_only"
    assert not load(ledger)["accepted"]
    target = workspace / "skills/source-check/SKILL.md"
    target.parent.mkdir(parents=True)
    before = b"---\nname: source-check\ndescription: Check sources\n---\nExisting constraints.\n"
    target.write_bytes(before)
    updated = propose(ledger, workspace, tmp_path / "update", name="source-check", environment="v1")
    assert target.read_bytes() == before and updated["before_sha256"]
    assert (tmp_path / "update/candidate/source-check/SKILL.md").read_bytes().startswith(before)
