"""B23.2：首尾编辑经过生产工具链，检查点/批准/诊断/撤销复用原事实。"""

from dataclasses import replace

import httpx
import pytest

from adam.config.settings import AgentConfig
from adam.contracts.errors import ApprovalDeniedError, BudgetError, EditLogError, StorageError
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from tests.features.test_diagnostic_request import observations
from tests.features.test_edit_diagnostics import structured_hooks
from tests.features.test_memory_context import config_at
from tests.features.test_span_edit_plan import arguments
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import application, names, settings
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def test_discovered_span_edit_is_recorded_undoable_and_replayed_without_writing(
    tmp_path, monkeypatch
):
    target = tmp_path / "sample.txt"
    raw = b"\xef\xbb\xbfbefore\r\nSTART\r\nold\r\nEND\r\nafter"
    target.write_bytes(raw)
    config = replace(config_at(tmp_path), tool_discovery=settings(tmp_path).tool_discovery)

    def respond(body, number):
        if number == 1:
            assert "replace_span" not in names(body)
            value = tool_response(
                [call("discover", "discover_tools", names=["read_file", "replace_span"])]
            )
        elif number == 2:
            assert "replace_span" in names(body)
            value = tool_response([call("read", "read_file", path="sample.txt")])
        elif number == 3:
            args = arguments(target, raw, expected_version=outputs(body)[-1]["version"])
            value = tool_response([call("span", "replace_span", **args)])
        else:
            assert outputs(body)[-1]["status"] == "applied"
            value = answer("changed")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "changed"
        assert target.read_bytes() == b"\xef\xbb\xbfbefore\r\nchanged\r\nafter"
        edit = store.load_edits(app.session.session_id)[0]
        assert edit["status"] == "applied" and edit["tool_call_id"] == "span"
        checkpoint = store.load_edit_checkpoint(
            app.session.session_id, edit["edit_id"], max_bytes=1024
        )
        assert checkpoint.original == raw and checkpoint.operation == "replace"
        assert (
            store.execution_report(app.session.session_id, edit["turn_id"]).actions[0].kind
            == "replace_span"
        )
        assert len(bodies) == 4
        assert app.undo().status == "applied" and target.read_bytes() == raw
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert target.read_bytes() == raw


@pytest.mark.parametrize(
    "boundary", ["denied", "plan", "external_before_plan", "external_in_approval"]
)
def test_span_limits_preserve_user_or_external_file(tmp_path, boundary):
    target = tmp_path / "sample.txt"
    raw = b"START x END"
    target.write_bytes(raw)
    approvals = []

    def approve(plan):
        approvals.append(plan)
        if boundary == "external_in_approval":
            target.write_bytes(b"external")
        return boundary != "denied"

    def respond(body, number):
        if number == 1:
            return tool_response([call("read", "read_file", path="sample.txt")])
        if number == 2:
            version = outputs(body)[-1]["version"]
            if boundary == "external_before_plan":
                target.write_bytes(b"external")
            return tool_response(
                [call("span", "replace_span", **arguments(target, raw, expected_version=version))]
            )
        assert not outputs(body)[-1]["ok"]
        if boundary == "plan":
            assert outputs(body)[-1]["status"] == "mode_blocked"
        return answer("not edited")

    with application(tmp_path, respond, config=config_at(tmp_path), approve=approve) as (
        app,
        store,
        _,
    ):
        if boundary == "plan":
            app.select_mode("plan")
        if boundary == "denied":
            with pytest.raises(ApprovalDeniedError):
                app.send("edit")
        else:
            assert app.send("edit") == "not edited"
        edits = store.load_edits(app.session.session_id)
        if boundary in {"denied", "external_in_approval"}:
            assert edits[0]["status"] == ("denied" if boundary == "denied" else "conflict")
            assert len(approvals) == 1
        else:
            assert edits == [] and approvals == []
    assert target.read_bytes() == (b"external" if boundary.startswith("external") else raw)


@pytest.mark.parametrize("protected", [False, True], ids=["syntax-report", "directory-denied"])
def test_span_uses_real_hook_policy_and_structured_diagnostics(tmp_path, protected):
    folder = tmp_path / "vendor" if protected else tmp_path
    folder.mkdir(exist_ok=True)
    target = folder / "sample.py"
    raw = b"# START\nvalue=1\n# END\n"
    target.write_bytes(raw)
    config = replace(config_at(tmp_path), hooks=structured_hooks())

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [
                    call(
                        "span",
                        "replace_span",
                        **arguments(
                            target, raw, start_text="# START", end_text="# END", new_text="value ="
                        ),
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["status"] == ("hook_blocked" if protected else "applied")
            if not protected:
                assert observations(body)[0]["reports"][0]["report"]["status"] == "failed"
            value = answer("done")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, _, _):
        assert recorder.run(lambda: app.send("task")) == "done"
        edits = store.load_edits(app.session.session_id)
        assert len(edits) == (0 if protected else 1)
        if not protected:
            assert edits[0]["status"] == "applied"
            assert (
                store.load_hooks(app.session.session_id)[-1]["diagnostics"]["expected_version"]
                == edits[0]["after_version"]
            )
    assert target.read_bytes() == (raw if protected else b"value =\n")


def test_span_shares_edit_attempt_budget_with_existing_tools(tmp_path):
    target = tmp_path / "sample.txt"
    raw = b"START x END"
    target.write_bytes(raw)
    config = replace(config_at(tmp_path), agent=AgentConfig(max_edit_attempts=1))

    def respond(body, number):
        assert number == 1
        return tool_response(
            [
                call("span", "replace_span", **arguments(target, raw)),
                call("create", "create_file", path="unexpected.txt", content="x"),
            ]
        )

    with application(tmp_path, respond, config=config, approve=lambda _: True) as (app, store, _):
        with pytest.raises(BudgetError):
            app.send("edit")
        assert len(store.load_edits(app.session.session_id)) == 1
    assert target.read_bytes() == b"changed" and not (tmp_path / "unexpected.txt").exists()


def test_span_result_save_failure_keeps_actual_edit_and_unknown_on_restart(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    raw = b"START x END"
    target.write_bytes(raw)

    def respond(body, number):
        assert number == 1
        return tool_response([call("span", "replace_span", **arguments(target, raw))])

    with application(tmp_path, respond, config=config_at(tmp_path), approve=lambda _: True) as (
        app,
        store,
        bodies,
    ):
        original = store.append_edit_event

        def save(sid, kind, payload):
            if kind == "edit_finished":
                raise StorageError("injected result save failure")
            original(sid, kind, payload)

        monkeypatch.setattr(store, "append_edit_event", save)
        with pytest.raises(EditLogError):
            app.send("edit")
        sid = app.session.session_id
        assert target.read_bytes() == b"changed" and len(bodies) == 1
    with SessionStore(config_at(tmp_path).sessions_dir) as reopened:
        reopened.select_session(sid)
        assert reopened.load_edits(sid)[0]["status"] == "unknown"
        assert reopened.load_messages(sid) == []
    assert target.read_bytes() == b"changed"
