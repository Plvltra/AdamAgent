"""B22.1.2：创建批准展示完整候选，批准前后真实路径变化不变成覆盖权限。"""

import pytest

from adam.cli.main import confirm_edit
from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import ToolError
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.policy import PathPolicy


def planner(root, confirm=None):
    return EditTools(PathPolicy(root, PermissionConfig()), ToolLimits(), confirm)


@pytest.mark.parametrize("answer,approved", [("y", True), ("Y", True), ("yes", False), ("", False)])
def test_create_confirmation_shows_exact_candidate_without_writing(tmp_path, answer, approved):
    shown, prompts = [], []

    def read(prompt):
        prompts.append(prompt)
        return answer

    tool = planner(tmp_path, lambda plan: confirm_edit(plan, read, shown.append))
    plan = tool.prepare_create({"path": "new.txt", "content": "完整\n候选"})
    assert tool.confirm_create(plan) is approved
    assert shown == [f"创建文件> {plan.path}", plan.diff]
    assert "--- /dev/null" in shown[1] and "+完整\n+候选" in shown[1]
    assert prompts == ["允许本次文件创建？[y/N] "]
    assert not plan.path.exists() and list(tmp_path.iterdir()) == []


def test_missing_approval_callback_cannot_authorize_create(tmp_path):
    tool = planner(tmp_path)
    plan = tool.prepare_create({"path": "new.txt", "content": ""})
    assert tool.confirm_create(plan) is False and not plan.path.exists()


@pytest.mark.parametrize("fault", [EOFError, KeyboardInterrupt, RuntimeError])
def test_confirmation_eof_denies_and_other_faults_propagate_without_writing(tmp_path, fault):
    def fail(_prompt):
        raise fault

    tool = planner(tmp_path, lambda plan: confirm_edit(plan, fail, lambda _: None))
    plan = tool.prepare_create({"path": "new.txt", "content": "x"})
    if fault is EOFError:
        assert tool.confirm_create(plan) is False
    else:
        with pytest.raises(fault):
            tool.confirm_create(plan)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("during_approval", [False, True])
def test_existing_file_before_or_during_approval_is_preserved(tmp_path, during_approval):
    calls = []

    def confirm(plan):
        calls.append(plan)
        plan.path.write_bytes(b"another writer")
        return True

    tool = planner(tmp_path, confirm)
    plan = tool.prepare_create({"path": "new.txt", "content": "our candidate"})
    if not during_approval:
        plan.path.write_bytes(b"another writer")
    with pytest.raises(ToolError, match="已经存在"):
        tool.confirm_create(plan)
    assert calls == ([plan] if during_approval else [])
    assert plan.path.read_bytes() == b"another writer"
    assert list(tmp_path.iterdir()) == [plan.path]


def test_removed_parent_during_approval_is_not_recreated(tmp_path):
    parent = tmp_path / "existing"
    parent.mkdir()

    def confirm(plan):
        plan.path.parent.rmdir()
        return True

    tool = planner(tmp_path, confirm)
    plan = tool.prepare_create({"path": "existing/new.txt", "content": "x"})
    with pytest.raises(ToolError, match="父目录"):
        tool.confirm_create(plan)
    assert not parent.exists()


def test_revoked_write_permission_during_approval_rejects_the_old_plan(tmp_path):
    workspace, outside = tmp_path / "work", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    def confirm(_plan):
        # 宿主替换当前策略模拟用户撤销授权；不是让模型修改权限。
        tool.policy = PathPolicy(workspace, PermissionConfig())
        return True

    tool = EditTools(
        PathPolicy(workspace, PermissionConfig(additional_write_dirs=(outside,))),
        ToolLimits(),
        confirm,
    )
    plan = tool.prepare_create({"path": str(outside / "new.txt"), "content": "x"})
    with pytest.raises(ToolError, match="additional_write_dirs"):
        tool.confirm_create(plan)
    assert list(outside.iterdir()) == []
