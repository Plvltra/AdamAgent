"""B20.2.1：实际编辑和检查之后，只读选择本轮诊断并核对文件版本与权限。"""

from dataclasses import replace
from hashlib import sha256

import httpx
import pytest

from adam.application.diagnostics import DiagnosticContextReader
from adam.config.hooks import HooksConfig
from adam.config.settings import AgentConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.diagnostic_context import MAX_CONTEXT_BYTES, selection_bytes
from adam.contracts.errors import RequestInterrupted, StorageError
from adam.session.storage import SessionStore
from adam.tool_calls.diagnostic_files import DiagnosticFiles
from adam.tool_calls.policy import PathPolicy
from tests.features.test_edit_diagnostics import structured_hooks
from tests.features.test_hook_acceptance import edit_model
from tests.features.test_hook_process import checker
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_hooks import hooked_app
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app


def version_reader(root, *, protected_dirs=(), cancel=None):
    return DiagnosticFiles(
        PathPolicy(root, PermissionConfig(), protected_dirs=protected_dirs), 1048576, cancel
    )


def batch_editor(paths, *, repair=False):
    """模型只提供动作，实际文件版本、编辑及检查均由生产路径产生。"""

    def respond(body, number):
        if number == 1:
            response = tool_response(
                [call(f"read-{i}", "read_file", path=str(p)) for i, p in enumerate(paths)]
            )
        elif number == 2:
            versions = {item["path"]: item["version"] for item in outputs(body)}
            response = tool_response(
                [
                    call(
                        f"edit-{i}",
                        "replace_text",
                        path=str(p),
                        old_text="value = 1",
                        new_text="value =",
                        expected_version=versions[str(p)],
                    )
                    for i, p in enumerate(paths)
                ]
            )
        elif number == 3 and repair:
            response = tool_response(
                [
                    call(
                        "repair",
                        "replace_text",
                        path=str(paths[0]),
                        old_text="value =",
                        new_text="value = 2",
                        expected_version=outputs(body)[-1]["after_version"],
                    )
                ]
            )
        else:
            response = answer("done")
        return httpx.Response(200, json=response)

    return respond


def test_latest_report_per_file_is_selected_without_modifying_old_facts(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    config = replace(config_at(tmp_path), hooks=structured_hooks())
    with captured_app(tmp_path, batch_editor([target], repair=True), config=config) as (
        app,
        recorder,
        store,
        _,
        _,
    ):
        assert recorder.run(lambda: app.send("task")) == "done"
        sid = app.session.session_id
        reports = [h for h in store.load_hooks(sid) if "diagnostics" in h]
        assert [h["diagnostics"]["status"] for h in reports] == ["failed", "passed"]
        turn_id = reports[-1]["turn_id"]
        before = store.load_events(sid)
        selected = DiagnosticContextReader(store, version_reader(tmp_path))(sid, turn_id)
        assert len(selected.reports) == 1 and selected.omitted_count == 0
        assert selected.reports[0].hook_id == reports[-1]["hook_id"]
        assert selected.reports[0].report.status == "passed"
        assert selected.reports[0].freshness == "current"
        assert store.load_events(sid) == before
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        assert DiagnosticContextReader(reopened, version_reader(tmp_path))(sid, turn_id) == selected
        assert reopened.load_events(sid) == before


def test_external_change_or_revoked_permission_cannot_reuse_old_error(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    with hooked_app(
        tmp_path, edit_model(target, "value =", []), structured_hooks(), approve=lambda _: True
    ) as (app, store):
        assert app.send("edit") == "done"
        sid = app.session.session_id
        turn_id = store.load_hooks(sid)[-1]["turn_id"]
        read = DiagnosticContextReader(store, version_reader(tmp_path))
        current = read(sid, turn_id)
        assert current.reports[0].freshness == "current"
        assert "invalid syntax" in current.messages()[0]["content"]
        target.write_text("value = 42\n")
        changed = read(sid, turn_id)
        assert changed.reports[0].freshness == "changed"
        assert changed.reports[0].report == current.reports[0].report
        assert "invalid syntax" not in changed.messages()[0]["content"]
        denied = DiagnosticContextReader(
            store, version_reader(tmp_path, protected_dirs=(tmp_path,))
        )
        assert denied(sid, turn_id).reports[0].freshness == "unavailable"
        target.unlink()
        assert read(sid, turn_id).reports[0].freshness == "unavailable"
        target.write_bytes(b"x" * 1048577)
        assert read(sid, turn_id).reports[0].freshness == "unavailable"
        # 没有本轮报告时不查询文件；取消只在实际观察需要访问文件时传播。
        cancel = CancellationToken()
        cancel.cancel()
        cancelled = DiagnosticContextReader(store, version_reader(tmp_path, cancel=cancel))
        assert cancelled(sid, "another-turn").reports == ()
        with pytest.raises(RequestInterrupted):
            cancelled(sid, turn_id)

        def failed_store(_):
            raise StorageError("injected read failure")

        monkeypatch.setattr(store, "load_hooks", failed_store)
        with pytest.raises(StorageError, match="injected"):
            read(sid, turn_id)


def test_only_four_recent_files_are_observed_and_omissions_are_explicit(tmp_path):
    paths = [tmp_path / f"sample-{i}.py" for i in range(6)]
    for path in paths:
        path.write_text("value = 1\n")
    config = replace(
        config_at(tmp_path), hooks=structured_hooks(), agent=AgentConfig(max_edit_attempts=8)
    )
    reads = []
    files = version_reader(tmp_path)

    def observe(path, expected):
        reads.append(path)
        return files(path, expected)

    with captured_app(tmp_path, batch_editor(paths), config=config) as (app, recorder, store, _, _):
        assert recorder.run(lambda: app.send("task")) == "done"
        sid = app.session.session_id
        selected = DiagnosticContextReader(store, observe)(
            sid, store.load_hooks(sid)[-1]["turn_id"]
        )
        assert [r.report.path for r in selected.reports] == [str(p) for p in paths[-4:]]
        assert selected.omitted_count == 2 and len(reads) == 4
        assert selection_bytes(selected.to_record()) <= MAX_CONTEXT_BYTES


def test_total_byte_budget_is_checked_before_observing_omitted_files(tmp_path):
    paths = [tmp_path / f"sample-{i}.py" for i in range(3)]
    for path in paths:
        path.write_text("value = 1\n")
    program = checker(
        tmp_path,
        "import json,sys\nd=json.load(sys.stdin)\nr=d['result']\n"
        "if r.get('status') == 'applied':\n"
        " item={'line':1,'column':1,'severity':'error','code':'Demo','message':'字'*500}\n"
        " out={'version':2,'note':'observed','diagnostics':{'checker':'demo','path':r['path'],"
        "'expected_version':r['after_version'],'checked_version':r['after_version'],"
        "'status':'failed','items':[item]*6}}\n"
        "else: out={'version':1,'note':'skip'}\n"
        "print(json.dumps(out,ensure_ascii=False))\n",
    )
    config = replace(
        config_at(tmp_path), hooks=HooksConfig(True, after_tool=program, max_output_bytes=65536)
    )
    reads = []

    def observe(path, _):
        reads.append(path)
        return "current"

    with captured_app(tmp_path, batch_editor(paths), config=config) as (app, recorder, store, _, _):
        assert recorder.run(lambda: app.send("task")) == "done"
        sid = app.session.session_id
        selected = DiagnosticContextReader(store, observe)(
            sid, store.load_hooks(sid)[-1]["turn_id"]
        )
        assert len(selected.reports) == 1 and selected.omitted_count == 2
        assert reads == [str(paths[-1])]
        assert selection_bytes(selected.to_record()) <= MAX_CONTEXT_BYTES


@pytest.mark.parametrize(
    "external", [b"value = 42\n", b"x" * 1048577], ids=["stale", "unavailable"]
)
def test_checker_could_not_check_edit_version_so_selection_does_not_recheck(
    tmp_path, monkeypatch, external
):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    with hooked_app(
        tmp_path, edit_model(target, "value =", []), structured_hooks(), approve=lambda _: True
    ) as (app, store):
        append = store.append_tool_event

        def save(sid, kind, payload):
            append(sid, kind, payload)
            if kind == "tool_finished" and payload["tool_call_id"] == "edit":
                target.write_bytes(external)

        monkeypatch.setattr(store, "append_tool_event", save)
        assert app.send("edit") == "done"
        sid = app.session.session_id

        def fail(*_):
            pytest.fail("uncheckable original report must not acquire new file evidence")

        selected = DiagnosticContextReader(store, fail)(sid, store.load_hooks(sid)[-1]["turn_id"])
        assert selected.reports[0].freshness == "not_checked"
        assert not selected.reports[0].report.items


def test_version_observer_respects_extra_read_roots_credentials_and_utf8(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    target = extra / "sample.py"
    raw = b"value = 1\n"
    target.write_bytes(raw)
    expected = sha256(raw).hexdigest()
    assert version_reader(workspace)(str(target), expected) == "unavailable"
    allowed = DiagnosticFiles(
        PathPolicy(workspace, PermissionConfig(additional_read_dirs=(extra,))), 1048576
    )
    assert allowed(str(target), expected) == "current"
    secret = extra / "llm.toml"
    secret.write_bytes(raw)
    assert allowed(str(secret), expected) == "unavailable"
    target.write_bytes(b"\xff")
    assert allowed(str(target), expected) == "unavailable"
