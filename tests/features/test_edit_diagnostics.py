"""B20.1：真实编辑和检查进程产生版本化诊断，原工具事实与重启记录保持。"""

import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from adam.config.hooks import HookCommand, HooksConfig
from adam.contracts.errors import HookLogError
from adam.session.storage import SessionStore
from tests.features.test_hook_acceptance import edit_model, example_hooks
from tests.features.test_hook_process import checker
from tests.features.test_subagents import outputs
from tests.features.test_tool_hooks import hooked_app

SCRIPT = Path(__file__).resolve().parents[2] / "examples/hooks/after_python_diagnostics.py"


def structured_hooks():
    return replace(example_hooks(), after_tool=HookCommand((sys.executable, str(SCRIPT))))


@pytest.mark.parametrize("replacement,expected", [("value = 2", "passed"), ("value =", "failed")])
def test_real_edit_and_checker_keep_distinct_outcomes_and_original_version(
    tmp_path, replacement, expected
):
    target = tmp_path / "sample.py"
    marker = tmp_path / "must-not-run"
    prefix = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    target.write_text(prefix + "value = 1\n", encoding="utf-8")
    bodies = []
    with hooked_app(
        tmp_path,
        edit_model(target, replacement, bodies),
        structured_hooks(),
        approve=lambda _: True,
    ) as (app, store):
        assert app.send("edit") == "done"
        sid = app.session.session_id
        assert store.load_edits(sid)[0]["status"] == "applied"
        hook = store.load_hooks(sid)[-1]
        assert hook["status"] == "observed" and hook["exit_code"] == 0
        diagnostic = hook["diagnostics"]
        assert diagnostic["status"] == expected
        assert (
            diagnostic["expected_version"]
            == diagnostic["checked_version"]
            == sha256(target.read_bytes()).hexdigest()
        )
        assert diagnostic["path"] == str(target.resolve()) and not marker.exists()
        if expected == "failed":
            assert diagnostic["items"][0]["line"] == 3
            assert diagnostic["items"][0]["severity"] == "error"
        else:
            assert not diagnostic["items"]
        # B20.2 独立观察追加在工具链后，原工具回包仍保留编辑事实，不混入检查状态。
        output = outputs(bodies[-1])[-1]
        assert output["status"] == "applied" and "diagnostics" not in output
        frozen = store.load_events(sid)
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == frozen
        assert reopened.load_hooks(sid)[-1]["diagnostics"] == diagnostic


@pytest.mark.parametrize(
    "external,status",
    [(b"value = 42\n", "stale"), (b"x" * 1048577, "unavailable")],
    ids=["changed", "oversized"],
)
def test_external_version_is_not_reported_as_the_edit_diagnostic(
    tmp_path, monkeypatch, external, status
):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    bodies = []
    with hooked_app(
        tmp_path, edit_model(target, "value =", bodies), structured_hooks(), approve=lambda _: True
    ) as (app, store):
        append = store.append_tool_event

        def save(sid, kind, payload):
            append(sid, kind, payload)
            if kind == "tool_finished" and payload["tool_call_id"] == "edit":
                target.write_bytes(external)

        monkeypatch.setattr(store, "append_tool_event", save)
        assert app.send("edit") == "done"
        diagnostic = store.load_hooks(app.session.session_id)[-1]["diagnostics"]
        assert diagnostic["status"] == status and not diagnostic["items"]
        assert store.load_edits(app.session.session_id)[0]["status"] == "applied"


def test_checker_cannot_attach_diagnostics_to_another_file(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    program = checker(
        tmp_path,
        "import json,sys\nd=json.load(sys.stdin)\nr=d['result']\n"
        "if r.get('status') == 'applied':\n"
        " print(json.dumps({'version':2,'note':'checked','diagnostics':{'checker':'fake',"
        "'path':'other.py','expected_version':r['after_version'],'checked_version':r['after_version'],"
        "'status':'passed','items':[]}}))\n"
        "else: print(json.dumps({'version':1,'note':'skip'}))\n",
    )
    with hooked_app(
        tmp_path,
        edit_model(target, "value =", []),
        HooksConfig(True, after_tool=program),
        approve=lambda _: True,
    ) as (app, store):
        assert app.send("edit") == "done"
        result = store.load_hooks(app.session.session_id)[-1]
        assert result["status"] == "failed" and result["error_code"] == "invalid_response"
        assert "diagnostics" not in result
        assert target.read_text() == "value =\n"


def test_log_rejects_diagnostic_source_tampering_without_losing_edit(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    bodies = []
    with hooked_app(
        tmp_path, edit_model(target, "value =", bodies), structured_hooks(), approve=lambda _: True
    ) as (app, store):
        append = store.append_hook_event

        def save(sid, kind, payload):
            if kind == "hook_finished" and "diagnostics" in payload["result"]:
                payload["result"]["diagnostics"]["path"] = "unrelated.py"
            return append(sid, kind, payload)

        monkeypatch.setattr(store, "append_hook_event", save)
        with pytest.raises(HookLogError):
            app.send("edit")
        assert target.read_text() == "value =\n"
        assert store.load_edits(app.session.session_id)[0]["status"] == "applied"
        assert store.load_hooks(app.session.session_id)[-1]["status"] == "in_progress"
        assert len(bodies) == 2
