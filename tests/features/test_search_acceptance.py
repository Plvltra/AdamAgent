"""B21.3：模型遇到真实目录变化后重新查询，及实际正则匹配中取消/重启。"""

import os
import subprocess
import time
from dataclasses import replace
from threading import Event, Thread

import httpx
import pytest

import adam.tool_calls.file_queries as queries
from adam.config.settings import ToolLimits
from adam.contracts.errors import RequestInterrupted
from adam.recording.runtime import replay_turn
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


def test_model_receives_stale_cursor_failure_then_explicitly_requeries_changed_directory(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle a")
    (workspace / "b.txt").write_text("needle b")
    pages = []

    def respond(body, phase):
        if phase == 1:
            value = tool_response(
                [call("first", "search_text", path="project", query="needle", limit=1)]
            )
        elif phase == 2:
            first = outputs(body)[-1]
            pages.append(first)
            # 在两次真实工具调用之间模拟外部新增文件；不是伪造游标失败。
            (workspace / "0.txt").write_text("needle new")
            value = tool_response(
                [
                    call(
                        "stale",
                        "search_text",
                        path="project",
                        query="needle",
                        cursor=first["next_cursor"],
                    )
                ]
            )
        elif phase == 3:
            failure = outputs(body)[-1]
            assert not failure["ok"] and "来源已变化" in failure["error"]
            assert "results" not in failure
            value = tool_response([call("fresh", "search_text", path="project", query="needle")])
        else:
            current = outputs(body)[-1]
            pages.append(current)
            assert [r["path"] for r in current["results"]] == ["0.txt", "a.txt", "b.txt"]
            assert not current["truncated"] and current["generation"] != pages[0]["generation"]
            value = answer("restarted search found all three")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "restarted search found all three"
        sid = app.session.session_id
        stable = store.load_events(sid)
        assert len([e for e in stable if e["type"] == "tool_finished"]) == 3
        assert len(bodies) == 4 and store.usage(sid) == (48, False)
        assert app.execution_report().status == "succeeded"
    with SessionStore(tmp_path / "sessions") as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == stable
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        queries.FileQueries, "run", lambda *a, **k: pytest.fail("replay scanned files")
    )
    assert replay_turn(source, tmp_path / "offline")["status"] == "matched"


def test_cancel_actual_regex_query_preserves_known_usage_and_restart_never_repeats_it(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "slow.txt").write_text("a" * 100000 + "!")
    config = replace(
        config_at(tmp_path),
        tool_limits=ToolLimits(regex_timeout_seconds=10, scan_timeout_seconds=15),
    )
    original_popen, original_match = subprocess.Popen, queries.match_lines
    processes, responses, tokens = [], [], []
    received, stop = Event(), Event()

    def match(*args, **kwargs):
        # 只观察生产传入的取消令牌；匹配器、工作进程和日志都实际执行。
        tokens.append(kwargs["cancel"])
        return original_match(*args, **kwargs)

    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        responses.append(kwargs["stdout"])
        return process

    def interrupt():
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not stop.is_set():
            # 第一个进程仅校验语法；等第二个实际文本进程已读入请求后才取消。
            if len(responses) >= 2 and os.fstat(responses[1].fileno()).st_size >= 2:
                received.set()
                tokens[-1].cancel()
                return
            time.sleep(0.01)

    def respond(body, phase):
        assert phase == 1, "cancelled query must not make another model request"
        return httpx.Response(
            200,
            json=tool_response(
                [call("slow", "search_text", path="project", query="(a+)+$", mode="regex")]
            ),
        )

    monkeypatch.setattr(queries, "match_lines", match)
    monkeypatch.setattr(subprocess, "Popen", capture)
    watcher = Thread(target=interrupt)
    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, _, bodies):
        watcher.start()
        try:
            with pytest.raises(RequestInterrupted):
                recorder.run(lambda: app.send("task"))
        finally:
            stop.set()
            watcher.join(timeout=3)
        assert received.is_set() and not watcher.is_alive()
        assert len(processes) == 2 and all(p.poll() is not None for p in processes)
        sid = app.session.session_id
        assert app.execution_report().status == "interrupted"
        assert store.usage(sid) == (12, False) and len(bodies) == 1
        assert [m.content for m in store.load_messages(sid)] == ["old question", "old answer"]
        stable = store.load_events(sid)
        # 取消沿原调度语义传播，没有成功结果就不伪造tool_finished；重启补中断证据。
        assert len([e for e in stable if e["type"] == "tool_started"]) == 1
        assert not any(e["type"] == "tool_finished" for e in stable)
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        recovered = reopened.load_events(sid)
        assert recovered[:-1] == stable and recovered[-1]["type"] == "session_recovered"
        report = read_report(next(config.sessions_dir.rglob("*.jsonl")))
        turn = report["turns"][-1]
        assert turn["status"] == "interrupted" and turn["tools"][0]["status"] == "interrupted"
        assert turn["tools"][0]["reason"] == "restart_recovery"
        assert reopened.usage(sid) == (12, False)
    with SessionStore(config.sessions_dir) as reopened:
        reopened.select_session(sid)
        assert reopened.load_events(sid) == recovered and reopened.usage(sid) == (12, False)
    assert len(processes) == 2 and len(bodies) == 1
