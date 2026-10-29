"""E6.1：实际目录及按需读取边界；读取元数据不要求正文已经打开。"""

import json

import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError
from adam.contracts.skills import validate_catalog
from adam.skills.catalog import SkillCatalog
from adam.tool_calls.policy import PathPolicy


def skill(root, identifier="verify-project", body="运行已发现的检查入口", resources=None):
    folder = root / "skills" / identifier
    folder.mkdir(parents=True)
    (folder / "skill.toml").write_text(
        'name = "检查项目"\ndescription = "修改后验证项目行为"\nresources = '
        + json.dumps(resources or [], ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "SKILL.md").write_text(body, encoding="utf-8")
    return folder


def catalog(root, **limits):
    return SkillCatalog(root, PathPolicy(root, PermissionConfig()).resolve, **limits)


def test_catalog_reads_metadata_only_and_load_returns_complete_versioned_material(tmp_path):
    folder = skill(tmp_path, resources=["reference.md"])
    (folder / "reference.md").write_text("按失败类型分组", encoding="utf-8")
    c = catalog(tmp_path)
    assert "content" not in c.summaries()[0]
    assert c.load("verify-project")["content"] == "运行已发现的检查入口"
    assert c.load("verify-project", "reference.md")["content"] == "按失败类型分组"
    assert len(c.load("verify-project")["version"]) == 64
    (folder / "SKILL.md").unlink()
    assert catalog(tmp_path).summaries() == c.summaries()
    with pytest.raises((ToolError, OSError)):
        c.load("verify-project")


@pytest.mark.parametrize("resource", ["../secret.md", "/secret.md", "unknown.md", "skill.toml"])
def test_unlisted_or_outside_material_is_rejected(tmp_path, resource):
    skill(tmp_path)
    with pytest.raises(ToolError):
        catalog(tmp_path).load("verify-project", resource)


def test_changed_metadata_and_oversized_body_fail_without_partial_guidance(tmp_path):
    folder = skill(tmp_path, body="x" * 10000)
    c = catalog(tmp_path, max_result_chars=1000)
    with pytest.raises(ToolError, match="结果上限"):
        c.load("verify-project")
    (folder / "skill.toml").write_text('name="changed"', encoding="utf-8")
    with pytest.raises(ToolError, match="元数据已变化"):
        c.load("verify-project")


def test_metadata_padding_is_normalized_before_context_validation(tmp_path):
    folder = skill(tmp_path)
    (folder / "skill.toml").write_text(
        'name = "' + " " * 81 + '检查项目  "\ndescription = " 修改后检查 "\nresources = []',
        encoding="utf-8",
    )
    summaries = catalog(tmp_path).summaries()
    validate_catalog(summaries)
    assert summaries[0]["name"] == "检查项目"
    assert summaries[0]["description"] == "修改后检查"


def test_material_link_cannot_escape_package(tmp_path):
    skill(tmp_path, resources=["linked.md"])
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    # 通过注入解析结果模拟联接解析；不依赖 Windows 创建符号链接的额外权限。
    policy = PathPolicy(tmp_path, PermissionConfig())
    c = SkillCatalog(tmp_path, lambda p: outside if p.endswith("linked.md") else policy.resolve(p))
    with pytest.raises(ToolError, match="链接"):
        c.load("verify-project", "linked.md")
