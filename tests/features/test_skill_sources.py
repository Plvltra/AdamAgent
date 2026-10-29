"""T3.1：两级目录覆盖、来源快照与专用读取授权；普通工具权限保持独立。"""

import json

import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError
from adam.contracts.skills import validate_catalog
from adam.skills.catalog import SkillCatalog
from adam.skills.library import SkillLibrary
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from tests.features.test_skill_catalog import skill
from tests.features.test_standard_skill_metadata import standard


def library_for(workspace, user_root):
    """只给用户技能加载器增加目录授权，主工具仍使用自己的 PathPolicy。"""
    project = SkillCatalog(workspace, PathPolicy(workspace, PermissionConfig()).resolve)
    user_policy = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(user_root,)))
    user = SkillCatalog(workspace, user_policy.resolve, root=user_root)
    return SkillLibrary(project, user)


@pytest.mark.parametrize("project_format", ["standard", "toml"])
def test_project_overrides_same_user_id_with_visible_source(tmp_path, project_format):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    workspace.mkdir()
    standard(user_home, "verify-project", body=b"USER_BODY")
    standard(user_home, "user-only", body=b"USER_ONLY")
    if project_format == "standard":
        standard(workspace, "verify-project", body=b"PROJECT_BODY")
    else:
        skill(workspace, body="PROJECT_BODY")
    library = library_for(workspace, user_home / "skills")
    summary = {item["id"]: item for item in library.summaries()}
    assert len(summary) == 2
    assert summary["verify-project"]["scope"] == "project"
    assert summary["verify-project"]["format"] == project_format
    assert summary["user-only"]["scope"] == "user"
    assert library.load("verify-project")["content"] == "PROJECT_BODY"
    assert library.load("user-only")["content"] == "USER_ONLY"
    overridden = [item for item in library.listings() if item.overridden_by]
    assert len(overridden) == 1 and overridden[0].scope == "user"
    assert overridden[0].overridden_by == summary["verify-project"]["source"]
    validate_catalog(library.summaries())


def test_user_skill_grant_does_not_expand_file_tools_or_allow_sibling_material(tmp_path):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    workspace.mkdir()
    source, _ = standard(user_home, body=b"AUTHORIZED_GUIDE")
    (user_home / "private.md").write_text("private", encoding="utf-8")
    library = library_for(workspace, user_home / "skills")
    assert library.load("inspect-code")["content"] == "AUTHORIZED_GUIDE"
    result = json.loads(
        create_tool_registry(workspace).execute("read_file", json.dumps({"path": str(source)}))
    )
    assert not result["ok"] and "未授权" in result["error"]
    with pytest.raises(ToolError):
        library.load("inspect-code", "../../private.md")
    policy = PathPolicy(workspace, PermissionConfig(additional_read_dirs=(user_home / "skills",)))
    with pytest.raises(ToolError, match="未授权"):
        policy.resolve(str(user_home / "private.md"))


def test_source_selection_is_frozen_until_refresh_without_fallback(tmp_path):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    workspace.mkdir()
    standard(user_home, body=b"GLOBAL")
    project_source, _ = standard(workspace, body=b"LOCAL")
    library = library_for(workspace, user_home / "skills")
    snapshot = library.summaries()
    project_source.unlink()
    with pytest.raises(ToolError):
        library.load("inspect-code")
    assert library.summaries() == snapshot
    standard(workspace, "added-later")
    with pytest.raises(ToolError, match="未登记"):
        library.load("added-later")
    # 恢复合法项目包后重建目录，新增项才可见；不把坏包静默忽略。
    standard(workspace, body=b"LOCAL_CHANGED")
    refreshed = library_for(workspace, user_home / "skills")
    assert refreshed.load("added-later")["content"] == "BODY_NOT_LOADED"
    assert refreshed.load("inspect-code")["content"] == "LOCAL_CHANGED"


def test_normalized_same_name_is_overridden_and_listing_is_immutable(tmp_path):
    workspace, user_home = tmp_path / "project", tmp_path / "user"
    workspace.mkdir()
    standard(user_home, "ｉｎｓｐｅｃｔ-code", body=b"USER")
    standard(workspace, body=b"PROJECT")
    library = library_for(workspace, user_home / "skills")
    summary = library.summaries()
    assert len(summary) == 1 and summary[0]["id"] == "inspect-code"
    assert library.load("inspect-code")["content"] == "PROJECT"
    summary[0]["source"] = "tampered"
    assert library.summaries()[0]["source"] != "tampered"
    assert len(library.listings()) == 2


def test_merged_directory_rejects_more_than_sixteen_unique_entries(tmp_path):
    workspace, user_home = tmp_path / "p", tmp_path / "u"
    workspace.mkdir()
    for index in range(9):
        standard(user_home, f"user-{index}")
    for index in range(8):
        standard(workspace, f"project-{index}")
    with pytest.raises(ToolError, match="16"):
        library_for(workspace, user_home / "skills")


def test_overridden_entries_do_not_double_the_active_count(tmp_path):
    workspace, user_home = tmp_path / "p", tmp_path / "u"
    for index in range(16):
        identifier = f"p-{index}"
        fields = f"name: {identifier}\ndescription: d"
        standard(workspace, identifier, fields=fields, body=b"project")
        standard(user_home, identifier, fields=fields, body=b"user")
    library = library_for(workspace, user_home / "skills")
    assert len(library.summaries()) == 16
    assert len(library.listings()) == 32
    assert all(item["scope"] == "project" for item in library.summaries())


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "remote"},
        {"source": "relative/SKILL.md"},
        {"source": ""},
        {"source": "bad\0path"},
    ],
)
def test_request_source_contract_rejects_invalid_provenance(tmp_path, change):
    workspace = tmp_path / "project"
    standard(workspace)
    library = library_for(workspace, tmp_path / "missing")
    item = library.summaries()[0]
    with pytest.raises(ValueError):
        validate_catalog([{**item, **change}])


def test_partial_source_and_modified_metadata_fail_explicitly(tmp_path):
    workspace = tmp_path / "project"
    source, header = standard(workspace)
    library = library_for(workspace, tmp_path / "missing")
    item = library.summaries()[0]
    item.pop("source")
    with pytest.raises(ValueError):
        validate_catalog([item])
    source.write_bytes(header.replace(b"Read code", b"Review code") + b"NEW_BODY")
    with pytest.raises(ToolError, match="元数据已变化"):
        library.load("inspect-code")


def test_same_directory_has_one_project_source_and_missing_directories_are_empty(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    assert library_for(workspace, tmp_path / "absent").summaries() == ()
    standard(workspace)
    c = SkillCatalog(workspace, PathPolicy(workspace, PermissionConfig()).resolve)
    library = SkillLibrary(c, c)
    assert len(library.listings()) == 1
    assert library.summaries()[0]["scope"] == "project"
