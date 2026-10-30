"""T5.3：运行仓库内真实 Hook 示例，组合验收工具权限、审计、故障和关闭配置。"""

import json
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from adam.config.hooks import HookCommand, parse_hooks
from adam.contracts.errors import ApprovalDeniedError, StorageError
from adam.session.diagnostics import read_report
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_hooks import hooked_app, model_for
from tests.features.test_tool_loop import answer, tool_response

PROJECT = Path(__file__).resolve().parents[2]


def example_hooks(*, enabled=True):
    """读取实际配置模板，仅替换测试解释器和脚本绝对路径，目录策略保持原样。"""
    data = tomllib.loads((PROJECT / "examples/hooks/config.toml").read_text(encoding="utf-8"))
    config = parse_hooks(data["hooks"])
    assert config.enabled is False
    commands = {}
    for event in ("before_tool", "after_tool"):
        argv = getattr(config, event).argv
        commands[event] = HookCommand((sys.executable, str(PROJECT / argv[1]), *argv[2:]))
    return replace(config, enabled=enabled, **commands)


def edit_model(target, replacement, payloads, expected="applied", reply="done"):
    """模拟先读取版本、再提出一次编辑的模型；实际编辑和检查由生产工具执行。"""

    def handle(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call("read", "read_file", path=str(target)),
                    ]
                ),
            )
        if len(payloads) == 2:
            return httpx.Response(
                200,
                json=tool_response(
                    [
                        call(
                            "edit",
                            "replace_text",
                            path=str(target),
                            old_text="value = 1",
                            new_text=replacement,
                            expected_version=outputs(body)[-1]["version"],
                        ),
                    ]
                ),
            )
        assert len(payloads) == 3 and outputs(body)[-1]["status"] == expected
        return httpx.Response(200, json=answer(reply))

    return handle


@pytest.mark.parametrize(
    "directory,replacement,approved,expected",
    [
        ("app", "value = 2", True, "applied"),
        ("app", "value =", True, "applied"),
        ("vendor", "value = 2", True, "hook_blocked"),
        ("vendor-copy", "value = 2", True, "applied"),
        ("generated", "value = 2", True, "hook_blocked"),
        ("app", "value = 2", False, "denied"),
    ],
)
def test_examples_check_actual_edit_and_preserve_permission_and_result(
    tmp_path, directory, replacement, approved, expected
):
    """语法检查使用真实修改字节；带顶层副作用的合法文件也不能被检查器执行。"""
    target = tmp_path / directory / "sample.py"
    target.parent.mkdir()
    marker = tmp_path / "python-code-ran"
    prefix = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    original = prefix + "value = 1\n"
    target.write_text(original, encoding="utf-8")
    payloads, notices, approvals = [], [], []

    handle = edit_model(target, replacement, payloads, expected)

    with hooked_app(
        tmp_path,
        handle,
        example_hooks(),
        notices=notices,
        approve=lambda plan: approvals.append(plan) or approved,
    ) as (app, store):
        if expected == "denied":
            with pytest.raises(ApprovalDeniedError):
                app.send("read and edit")
        else:
            assert app.send("read and edit") == "done"
        sid = app.session.session_id
        edits = store.load_edits(sid)
        hook_records = [r for r in store.load_hooks(sid) if r["tool_call_id"] == "edit"]
        syntax_failed = expected == "applied" and replacement == "value ="
        assert len(approvals) == (expected != "hook_blocked")
        assert not marker.exists()
        assert target.read_text(encoding="utf-8") == (
            prefix + replacement + "\n" if expected == "applied" else original
        )
        if expected == "hook_blocked":
            assert edits == [] and [r["status"] for r in hook_records] == ["denied"]
        else:
            assert edits[0]["status"] == expected
            assert [r["status"] for r in hook_records] == [
                "allowed",
                "failed" if syntax_failed else "observed",
            ]
            assert hook_records[-1]["error_code"] == ("nonzero_exit" if syntax_failed else "")
            if syntax_failed:
                assert "语法检查失败" in hook_records[-1]["stderr"]
                assert any("后置 Hook 检查失败" in message for message in notices)
        path = next(store.root.rglob(sid + ".jsonl"))
        before = path.read_bytes()
        report = read_report(path)
        assert report["turns"][0]["hooks"] == store.load_hooks(sid)
        assert path.read_bytes() == before
        assert {r["tool_call_id"] for r in report["turns"][0]["hooks"]} <= {"read", "edit"}


@pytest.mark.parametrize(
    "external", [b"value = 42\n", b"#" * 1048577], ids=["changed", "oversized"]
)
def test_after_example_rejects_external_change_instead_of_checking_a_different_version(
    tmp_path, monkeypatch, external
):
    """在实际工具结果保存后模拟外部编辑；后置检查必须拒绝把不同文件版本当作成功依据。"""
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    payloads, notices = [], []
    with hooked_app(
        tmp_path,
        edit_model(target, "value = 2", payloads),
        example_hooks(),
        notices=notices,
        approve=lambda _: True,
    ) as (app, store):
        append = store.append_tool_event

        def after_save(session_id, kind, payload):
            append(session_id, kind, payload)
            if kind == "tool_finished" and payload["tool_call_id"] == "edit":
                target.write_bytes(external)

        monkeypatch.setattr(store, "append_tool_event", after_save)
        assert app.send("edit") == "done"
        sid = app.session.session_id
        assert target.read_bytes() == external
        assert store.load_edits(sid)[0]["status"] == "applied"
        result = store.load_hooks(sid)[-1]
        assert result["status"] == "failed" and result["exit_code"] == 2
        assert any("后置 Hook 检查失败" in notice for notice in notices)
        assert len(payloads) == 3


def test_disabled_config_runs_original_edit_without_starting_any_checker(tmp_path, monkeypatch):
    import adam.hooks.runner as runner

    def forbidden(*args, **kwargs):
        pytest.fail("disabled hook must not start a program")

    monkeypatch.setattr(runner, "run_process", forbidden)
    target = tmp_path / "vendor" / "sample.py"
    target.parent.mkdir()
    target.write_text("value = 1\n")
    payloads = []

    handle = edit_model(target, "value =", payloads)

    with hooked_app(tmp_path, handle, example_hooks(enabled=False), approve=lambda _: True) as (
        app,
        store,
    ):
        assert app.send("edit") == "done"
        assert target.read_text() == "value =\n"
        assert store.load_hooks(app.session.session_id) == []
        assert len(store.load_edits(app.session.session_id)) == 1


def test_plan_prevents_edit_before_hook_and_approval(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    payloads = []

    handle = edit_model(target, "value = 2", payloads, "mode_blocked", "plan")

    with hooked_app(
        tmp_path, handle, example_hooks(), approve=lambda _: pytest.fail("plan cannot approve")
    ) as (app, store):
        app.select_mode("plan")
        assert app.send("investigate") == "plan"
        records = store.load_hooks(app.session.session_id)
        assert len(records) == 2 and {r["tool_call_id"] for r in records} == {"read"}
        assert {r["execution_mode"] for r in records} == {"plan"}
        assert target.read_text() == "value = 1\n"


def test_new_path_rules_are_supplied_before_any_hook_for_that_call(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Use local conventions", encoding="utf-8")
    (nested / "note.txt").write_text("evidence")
    payloads = []

    def handle(request):
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(
                200, json=tool_response([call("deferred", "read_file", path="nested/note.txt")])
            )
        if len(payloads) == 2:
            assert outputs(body)[-1]["status"] == "deferred"
            assert "Use local conventions" in json.dumps(body)
            return httpx.Response(
                200, json=tool_response([call("ready", "read_file", path="nested/note.txt")])
            )
        assert outputs(body)[-1]["ok"]
        return httpx.Response(200, json=answer("done"))

    with hooked_app(tmp_path, handle, example_hooks()) as (app, store):
        assert app.send("read nested file") == "done"
        records = store.load_hooks(app.session.session_id)
        assert len(records) == 2 and {r["tool_call_id"] for r in records} == {"ready"}


def test_tool_result_storage_failure_prevents_after_hook(tmp_path, monkeypatch):
    """故障位于工具结果写入边界，检查后置程序不会观察未可靠保存的结果。"""
    (tmp_path / "note.txt").write_text("evidence")
    payloads = []
    with hooked_app(
        tmp_path, model_for([call("read", "read_file", path="note.txt")], payloads), example_hooks()
    ) as (app, store):
        append = store.append_tool_event

        def fail(session_id, kind, payload):
            if kind == "tool_finished":
                raise StorageError("simulated disk failure")
            append(session_id, kind, payload)

        monkeypatch.setattr(store, "append_tool_event", fail)
        with pytest.raises(StorageError):
            app.send("read")
        sid = app.session.session_id
        assert len(payloads) == 1 and store.load_messages(sid) == []
        assert [r["event"] for r in store.load_hooks(sid)] == ["before_tool"]


def test_user_interface_failure_after_hook_preserves_edit_and_audit(tmp_path):
    """展示故障向上传播，不能由注册表伪装成可自动重试的工具参数错误。"""

    class BrokenNotices(list):
        def append(self, message):
            if "后置 Hook 检查失败" in message:
                raise RuntimeError("terminal failure")
            super().append(message)

    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    payloads = []

    handle = edit_model(target, "value =", payloads)

    with hooked_app(
        tmp_path, handle, example_hooks(), notices=BrokenNotices(), approve=lambda _: True
    ) as (app, store):
        with pytest.raises(RuntimeError, match="terminal failure"):
            app.send("edit")
        sid = app.session.session_id
        assert target.read_text() == "value =\n"
        assert store.load_edits(sid)[0]["status"] == "applied"
        assert store.load_hooks(sid)[-1]["status"] == "failed"
        assert store.load_messages(sid) == [] and len(payloads) == 2
