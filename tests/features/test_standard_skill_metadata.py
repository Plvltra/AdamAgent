"""T2.1：真实 SKILL.md 的标准头部、目录发现与受限解析；不执行技能正文。"""

from hashlib import sha256

import pytest

from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError
from adam.skills.discovery import discover_packages
from adam.skills.frontmatter import read_header
from adam.tool_calls.policy import PathPolicy


def standard(
    root, identifier="inspect-code", *, fields=None, body=b"BODY_NOT_LOADED", newline=b"\n"
):
    package = root / "skills" / identifier
    package.mkdir(parents=True, exist_ok=True)
    fields = fields or f"name: {identifier}\ndescription: Read code when investigating a function"
    header = ("---\n" + fields + "\n---\n").encode("utf-8").replace(b"\n", newline)
    source = package / "SKILL.md"
    source.write_bytes(header + body)
    return source, header


def discover(root):
    return discover_packages(root / "skills", PathPolicy(root, PermissionConfig()).resolve)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_standard_metadata_discovery_never_reads_body_and_preserves_optional_fields(
    tmp_path, newline
):
    source, raw = standard(
        tmp_path,
        newline=newline,
        fields="name: inspect-code\ndescription: >\n  Investigate definitions\n  and references.\n"
        "license: MIT\ncompatibility: Python 3.11+\nallowed-tools: run_command\n"
        "metadata:\n  author: tester\n  version: '1.0'\nhooks:\n  before: do-not-execute",
        body=b"\xff" * 20000,
    )
    found = discover(tmp_path)
    assert len(found) == 1 and found[0].format == "standard"
    header = found[0].header
    assert header.name == "inspect-code"
    assert header.description == "Investigate definitions and references."
    assert header.version == sha256(raw).hexdigest()
    assert header.body_offset == len(raw)
    assert header.metadata["metadata"]["version"] == "1.0"
    assert header.metadata["allowed-tools"] == "run_command"
    assert header.metadata["hooks"] == {"before": "do-not-execute"}
    assert source.read_bytes()[header.body_offset :] == b"\xff" * 20000


@pytest.mark.parametrize("identifier", ["检查代码", "réviser-code", "ｉｎｓｐｅｃｔ-code"])
def test_unicode_name_uses_reference_normalization(tmp_path, identifier):
    from adam.contracts.skills import standard_skill_name

    standard(tmp_path, identifier)
    assert discover(tmp_path)[0].header.name == standard_skill_name(identifier)


def test_normalized_names_cannot_hide_duplicate_packages(tmp_path):
    standard(tmp_path, "inspect-code")
    standard(tmp_path, "ｉｎｓｐｅｃｔ-code")
    with pytest.raises(ToolError, match="规范化后重复"):
        discover(tmp_path)


def test_name_description_limits_bom_and_body_version_are_independent(tmp_path):
    source, raw = standard(tmp_path, "1-code", fields="name: 1-code\ndescription: " + "x" * 1024)
    source.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
    first = read_header(source, "1-code")
    assert first.version == sha256(b"\xef\xbb\xbf" + raw).hexdigest()
    source.write_bytes(b"\xef\xbb\xbf" + raw + b"CHANGED_BODY")
    assert read_header(source, "1-code") == first


@pytest.mark.parametrize(
    "fields",
    [
        "name: Inspect-Code\ndescription: test",
        "name: inspect--code\ndescription: test",
        "name: other\ndescription: test",
        "name: inspect-code\ndescription: ''",
        "name: inspect-code\ndescription: " + "x" * 1025,
        "name: inspect-code\ndescription: true",
        "name: inspect-code\ndescription: test\nmetadata: {version: 1}",
        "name: inspect-code\ndescription: test\ncompatibility: " + "x" * 501,
        "name: inspect-code\ndescription: test\nallowed-tools: [run_command]",
    ],
)
def test_invalid_standard_fields_are_rejected(tmp_path, fields):
    standard(tmp_path, fields=fields)
    with pytest.raises(ToolError):
        discover(tmp_path)


@pytest.mark.parametrize(
    "fields",
    [
        "name: inspect-code\nname: other\ndescription: test",
        "name: inspect-code\ndescription: test\nmetadata: {author: a, author: b}",
        "name: inspect-code\ndescription: &desc test\nextra: *desc",
        "name: inspect-code\ndescription: test\nextra: !!python/object/apply:builtins.eval ['1+1']",
        "name: inspect-code\ndescription: [bad",
        "name: inspect-code\ndescription: test\nextra: " + "[" * 12 + "0" + "]" * 12,
        "name: inspect-code\ndescription: test\nextra: .nan",
    ],
)
def test_duplicate_alias_unsafe_tag_and_deep_yaml_are_rejected(tmp_path, fields):
    standard(tmp_path, fields=fields)
    with pytest.raises(ToolError):
        discover(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        b"name: inspect-code\ndescription: test\n",
        b"---\nname: inspect-code\ndescription: test\n",
        b"---\nname: inspect-code\ndescription: \xff\n---\n",
        b"---\nname: inspect-code\ndescription: bad\x00text\n---\n",
        b"---\n" + b"#" * 8200 + b"\n---\n",
    ],
)
def test_unbounded_or_malformed_header_is_not_treated_as_legacy_body(tmp_path, raw):
    source, _ = standard(tmp_path)
    source.write_bytes(raw)
    with pytest.raises(ToolError):
        discover(tmp_path)


def test_package_sources_are_unique_and_legacy_metadata_stays_readable(tmp_path):
    source, _ = standard(tmp_path)
    (source.parent / "skill.toml").write_text('name="old"', encoding="utf-8")
    with pytest.raises(ToolError, match="一种明确来源"):
        discover(tmp_path)
    source.write_text("legacy markdown body", encoding="utf-8")
    assert discover(tmp_path)[0].format == "toml"
    source.unlink()
    assert discover(tmp_path)[0].format == "toml"


def test_package_link_cannot_read_external_header(tmp_path):
    source, raw = standard(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(raw + b"private")
    policy = PathPolicy(tmp_path, PermissionConfig())

    # 注入解析结果模拟 Windows 联接，验证读取前的包边界，无需创建链接权限。
    def redirect(value):
        return outside if value == str(source) else policy.resolve(value)

    with pytest.raises(ToolError, match="包内"):
        discover_packages(tmp_path / "skills", redirect)


def test_discovery_limits_package_count_and_empty_root_is_allowed(tmp_path):
    assert discover(tmp_path) == ()
    for index in range(17):
        standard(tmp_path, f"skill-{index}")
    with pytest.raises(ToolError, match="16项"):
        discover(tmp_path)
