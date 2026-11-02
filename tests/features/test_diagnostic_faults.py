"""B20.3：诊断超限、真实检查取消及日志 fsync 故障，不能抹掉已经成功的编辑。"""

import os
import threading
import time

import pytest

from adam.config.hooks import HooksConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import HookLogError, RequestInterrupted, StorageError
from adam.hooks.runner import HookRunner
from adam.session.storage import SessionStore
from tests.features.test_cancellation import alive
from tests.features.test_diagnostic_request import observations, request_contexts
from tests.features.test_edit_diagnostics import structured_hooks
from tests.features.test_hook_acceptance import edit_model
from tests.features.test_hook_process import checker
from tests.features.test_subagents import outputs
from tests.features.test_tool_hooks import hooked_app


@pytest.mark.parametrize(
    "output_limit,expected", [(512, "output_limit"), (65536, "invalid_response")]
)
def test_actual_oversized_diagnostic_never_becomes_partial_current_findings(
    tmp_path, output_limit, expected
):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    program = checker(
        tmp_path,
        "import json,sys\nd=json.load(sys.stdin)\nr=d['result']\n"
        "if r.get('status') == 'applied':\n"
        " item={'line':1,'column':1,'severity':'error','code':'Demo','message':'字'*500}\n"
        " out={'version':2,'note':'observed','diagnostics':{'checker':'demo','path':r['path'],"
        "'expected_version':r['after_version'],'checked_version':r['after_version'],"
        "'status':'failed','items':[item]*12}}\n"
        "else: out={'version':1,'note':'skip'}\n"
        "print(json.dumps(out,ensure_ascii=False))\n",
    )
    bodies, notices = [], []
    with hooked_app(
        tmp_path,
        edit_model(target, "value =", bodies),
        HooksConfig(True, after_tool=program, max_output_bytes=output_limit),
        approve=lambda _: True,
        notices=notices,
    ) as (app, store):
        assert app.send("edit") == "done"
        sid = app.session.session_id
        hook = store.load_hooks(sid)[-1]
        assert hook["status"] == "failed" and hook["error_code"] == expected
        assert "diagnostics" not in hook and observations(bodies[-1]) == []
        assert outputs(bodies[-1])[-1]["status"] == "applied"
        assert store.load_edits(sid)[0]["status"] == "applied"
        assert any("后置 Hook 检查失败" in notice for notice in notices)
        frozen = store.load_events(sid)
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == frozen
        assert target.read_text() == "value =\n" and len(bodies) == 3


@pytest.mark.parametrize("boundary", ["hook_report", "request_context"])
def test_fsync_failure_rolls_back_only_new_record_and_restart_never_reedits(
    tmp_path, monkeypatch, boundary
):
    """在指定记录真实写入后使第一次 fsync 失败，回滚的 fsync 仍可执行。"""
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    bodies = []
    with hooked_app(
        tmp_path, edit_model(target, "value =", bodies), structured_hooks(), approve=lambda _: True
    ) as (app, store):
        fsync = os.fsync
        failures = []

        def fail_once(fd):
            if not failures:
                failures.append(True)
                raise OSError("injected diagnostic fsync failure")
            return fsync(fd)

        if boundary == "hook_report":
            append = store.append_hook_event

            def save(sid, kind, payload):
                if kind == "hook_finished" and "diagnostics" in payload["result"]:
                    with monkeypatch.context() as patch:
                        patch.setattr(os, "fsync", fail_once)
                        return append(sid, kind, payload)
                return append(sid, kind, payload)

            monkeypatch.setattr(store, "append_hook_event", save)
        else:
            start = store.start_request

            def start_checked(sid, tid, model, *, context=None):
                if context["diagnostics"]["reports"]:
                    with monkeypatch.context() as patch:
                        patch.setattr(os, "fsync", fail_once)
                        return start(sid, tid, model, context=context)
                return start(sid, tid, model, context=context)

            monkeypatch.setattr(store, "start_request", start_checked)
        with pytest.raises(HookLogError if boundary == "hook_report" else StorageError):
            app.send("edit")
        sid = app.session.session_id
        assert failures == [True] and len(bodies) == 2
        assert len(request_contexts(store, sid)) == 2
        assert store.load_edits(sid)[0]["status"] == "applied"
        assert target.read_text() == "value =\n"
        assert app.execution_report().status == "failed"
        assert store.load_messages(sid) == []
        assert store.usage(sid) == (24, False)
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        hook = reopened.load_hooks(sid)[-1]
        if boundary == "hook_report":
            assert hook["status"] == "interrupted" and hook["error_code"] == "restart_recovery"
            assert "diagnostics" not in hook
        else:
            assert hook["status"] == "observed" and hook["diagnostics"]["status"] == "failed"
        stable = reopened.load_events(sid)
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == stable
        assert len(reopened.load_edits(sid)) == 1
        assert target.read_text() == "value =\n" and len(bodies) == 2


def test_cancel_while_real_diagnostic_checker_runs_reaps_it_and_retains_edit(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("value = 1\n")
    ready = tmp_path / "checker.pid"
    program = checker(
        tmp_path,
        "import json,os,sys,time\nfrom pathlib import Path\nd=json.load(sys.stdin)\n"
        "if d['result'].get('status') == 'applied':\n"
        f" Path({str(ready)!r}).write_text(str(os.getpid()))\n time.sleep(30)\n"
        "else: print(json.dumps({'version':1,'note':'skip'}))\n",
    )
    cancel, failures, bodies = CancellationToken(), [], []
    # 只给生产检查器注入本测试的取消令牌；进程、编辑和日志仍走真实装配路径。
    monkeypatch.setattr(
        "adam.bootstrap.HookRunner", lambda cfg, workspace: HookRunner(cfg, workspace, cancel)
    )

    def stop():
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            failures.append("checker did not start")
        cancel.cancel()

    watcher = threading.Thread(target=stop)
    with hooked_app(
        tmp_path,
        edit_model(target, "value =", bodies),
        HooksConfig(True, after_tool=program, timeout_seconds=20),
        approve=lambda _: True,
    ) as (app, store):
        watcher.start()
        try:
            with pytest.raises(RequestInterrupted):
                app.send("edit")
        finally:
            cancel.cancel()
            watcher.join(12)
        assert not watcher.is_alive() and not failures
        assert not alive(int(ready.read_text()))
        sid = app.session.session_id
        hook = store.load_hooks(sid)[-1]
        assert hook["status"] == "interrupted" and hook["error_code"] == "cancelled"
        assert "diagnostics" not in hook
        assert store.load_edits(sid)[0]["status"] == "applied"
        assert len(bodies) == 2 and len(request_contexts(store, sid)) == 2
        assert store.usage(sid) == (24, False)
        assert app.execution_report().status == "interrupted"
        frozen = store.load_events(sid)
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == frozen
        assert target.read_text() == "value =\n" and len(bodies) == 2
