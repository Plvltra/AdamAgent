"""T1：真实文件规则发现、作用域与失败边界，后续在同一文件补上下文业务链。"""

from functools import partial

import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError
from adam.contracts.rules import validate_rules
from adam.rules.catalog import RuleCatalog
from adam.tool_calls.policy import PathPolicy


def catalog(root):
    return RuleCatalog(
        root, partial(PathPolicy(root, PermissionConfig()).resolve, must_exist=False)
    )


def write_rule(directory, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENTS.md").write_text(text, encoding="utf-8")


def test_nested_rules_keep_scope_and_never_scan_unrelated_directories(tmp_path):
    write_rule(tmp_path, "ROOT")
    write_rule(tmp_path / "src", "SOURCE")
    write_rule(tmp_path / "tests", "TEST")
    write_rule(tmp_path / "unused", "UNUSED")
    selection = catalog(tmp_path).select((tmp_path / "src" / "new.py", tmp_path / "tests"))
    validate_rules(selection.to_record())
    assert [doc.scope for doc in selection.documents] == [".", "src", "tests"]
    assert [doc.content for doc in selection.applicable("src/new.py")] == ["ROOT", "SOURCE"]
    assert [doc.content for doc in selection.applicable("tests/test_file.py")] == ["ROOT", "TEST"]
    assert [doc.content for doc in selection.applicable("README.md")] == ["ROOT"]
    assert "UNUSED" not in selection.messages()[0]["content"]
    assert selection.messages()[0]["role"] == "user"


def test_rule_changes_and_deletion_do_not_rewrite_previous_snapshot(tmp_path):
    write_rule(tmp_path, "before")
    rules = catalog(tmp_path)
    previous = rules.select()
    write_rule(tmp_path, "after")
    current = rules.select()
    assert previous.documents[0].version != current.documents[0].version
    assert previous.documents[0].content == "before"
    (tmp_path / "AGENTS.md").unlink()
    assert rules.select().documents == ()


@pytest.mark.parametrize("raw", [b"\xff", b"bad\0text", b"x" * 16385])
def test_invalid_rule_is_not_silently_omitted_or_truncated(tmp_path, raw):
    (tmp_path / "AGENTS.md").write_bytes(raw)
    with pytest.raises(ToolError):
        catalog(tmp_path).select()


def test_total_rule_limit_is_checked_across_distinct_scopes(tmp_path):
    targets = []
    for index in range(5):
        directory = tmp_path / str(index)
        write_rule(directory, "x" * 14000)
        targets.append(directory)
    with pytest.raises(ToolError, match="64 KiB"):
        catalog(tmp_path).select(tuple(targets))


def test_rule_link_and_protected_directory_are_rejected(tmp_path):
    write_rule(tmp_path, "root")
    outside = tmp_path / "other.md"
    outside.write_text("not a rule", encoding="utf-8")
    # 用真实解析结果替身覆盖联接边界，不依赖 Windows 的符号链接创建权限。
    with pytest.raises(ToolError, match="链接"):
        RuleCatalog(tmp_path, lambda _: outside).select()
    protected = tmp_path / "private"
    write_rule(protected, "private")
    policy = PathPolicy(tmp_path, PermissionConfig(), protected_dirs=(protected,))
    with pytest.raises(ToolError, match="保留目录"):
        RuleCatalog(tmp_path, partial(policy.resolve, must_exist=False)).select((protected,))


def test_snapshot_validation_rejects_scope_forgery_and_duplicates(tmp_path):
    write_rule(tmp_path, "root")
    record = catalog(tmp_path).select().to_record()
    record["documents"][0]["scope"] = "../outside"
    with pytest.raises(ValueError):
        validate_rules(record)
    record = catalog(tmp_path).select().to_record()
    record["documents"].append(record["documents"][0])
    with pytest.raises(ValueError):
        validate_rules(record)
