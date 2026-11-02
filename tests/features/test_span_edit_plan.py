"""B23.1：首尾精确定位只生成原编辑计划，保留真实版本、权限和原始字节。"""

from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.edit_plan import EditConflictError
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.file_evidence import FileStateError
from adam.tool_calls.policy import PathPolicy


def planner(root, maximum=100000, permissions=None):
    return EditTools(
        PathPolicy(root, permissions or PermissionConfig()),
        ToolLimits(max_text_file_bytes=maximum),
        lambda _: pytest.fail("planning must not ask for approval"),
    )


def arguments(path, raw, **changes):
    return {
        "path": str(path),
        "start_text": "START",
        "end_text": "END",
        "new_text": "changed",
        "expected_version": sha256(raw).hexdigest(),
        **changes,
    }


@pytest.mark.parametrize(
    "prefix,newline",
    [(b"", "\n"), (b"", "\r\n"), (b"\xef\xbb\xbf", "\r\n")],
    ids=["lf", "crlf", "bom-crlf"],
)
def test_span_plan_preserves_original_bytes_and_replaces_inclusive_boundaries(
    tmp_path, prefix, newline
):
    text = "before\nSTART\nold\nEND\nafter"
    raw = prefix + text.replace("\n", newline).encode()
    path = tmp_path / "sample.txt"
    path.write_bytes(raw)
    plan = planner(tmp_path).prepare_span(arguments(path, raw, new_text="new\r\nblock"))
    assert plan.candidate == prefix + "before\nnew\nblock\nafter".replace("\n", newline).encode()
    assert plan.original == raw and plan.before_version == sha256(raw).hexdigest()
    assert "-START" in plan.diff and "-END" in plan.diff and "+block" in plan.diff
    assert path.read_bytes() == raw and list(tmp_path.iterdir()) == [path]
    with pytest.raises(FrozenInstanceError):
        plan.candidate = b"other"


def test_empty_replacement_deletes_span_and_multiline_anchors_can_cover_whole_file(tmp_path):
    path = tmp_path / "sample.txt"
    raw = b"START\nbody\nEND"
    path.write_bytes(raw)
    plan = planner(tmp_path).prepare_span(
        arguments(path, raw, start_text="START\n", end_text="\nEND", new_text="")
    )
    assert plan.candidate == b"" and "No newline at end of file" in plan.diff
    assert plan.original == raw and path.read_bytes() == raw


@pytest.mark.parametrize(
    "text,start,end",
    [
        ("START x END", "", "END"),
        ("START x END", "START", ""),
        ("START x END", "missing", "END"),
        ("START x END", "START", "missing"),
        ("START START x END", "START", "END"),
        ("START x END END", "START", "END"),
        ("END x START", "START", "END"),
        ("START", "START", "START"),
        ("abcdef", "abcde", "def"),
        ("aaaa end", "aaa", "end"),
        ("START\r\nx\nEND", "START", "END"),
    ],
    ids=[
        "empty-start",
        "empty-end",
        "missing-start",
        "missing-end",
        "repeated-start",
        "repeated-end",
        "reversed",
        "same",
        "overlap",
        "overlapping-matches",
        "mixed-newline",
    ],
)
def test_invalid_or_ambiguous_boundaries_are_rejected_without_side_effect(
    tmp_path, text, start, end
):
    raw = text.encode()
    path = tmp_path / "sample.txt"
    path.write_bytes(raw)
    with pytest.raises(FileStateError) as raised:
        planner(tmp_path).prepare_span(arguments(path, raw, start_text=start, end_text=end))
    assert raised.value.version == sha256(raw).hexdigest()
    assert path.read_bytes() == raw


def test_version_conflict_precedes_anchor_selection_and_keeps_actual_version(tmp_path):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"external content")
    with pytest.raises(EditConflictError) as raised:
        planner(tmp_path).prepare_span(arguments(path, b"START x END"))
    assert raised.value.version == sha256(path.read_bytes()).hexdigest()


def test_no_change_anchor_limit_and_candidate_limit_do_not_write(tmp_path):
    path = tmp_path / "sample.txt"
    raw = b"START x END"
    path.write_bytes(raw)
    for changes, maximum in [
        ({"new_text": raw.decode()}, 100000),
        ({"start_text": "x" * 4097}, 100000),
        ({"new_text": "y" * 40}, 20),
    ]:
        with pytest.raises(FileStateError):
            planner(tmp_path, maximum).prepare_span(arguments(path, raw, **changes))
    assert path.read_bytes() == raw


def test_read_only_authorization_does_not_allow_span_edit_planning(tmp_path):
    workspace, outside = tmp_path / "work", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    path = outside / "sample.txt"
    raw = b"START x END"
    path.write_bytes(raw)
    with pytest.raises(ToolError, match="additional_write_dirs"):
        planner(workspace, permissions=PermissionConfig((outside,), ())).prepare_span(
            arguments(path, raw)
        )
    allowed = planner(workspace, permissions=PermissionConfig((), (outside,)))
    assert allowed.prepare_span(arguments(path, raw)).candidate == b"changed"
    assert path.read_bytes() == raw
    assert all(
        tool["function"]["name"] != "replace_span"
        for tool in create_tool_registry(workspace).definitions()
    )
