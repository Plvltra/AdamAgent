"""T6.3：真实 Git/文件查询、规则、Hook 与预算经过生产组装后的组合验收。"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from threading import Barrier, Lock, current_thread

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.hooks import HooksConfig
from adam.config.settings import ReflectionConfig
from adam.contracts.errors import BudgetError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from adam.tool_calls.file_tools import FileTools
from adam.tool_calls.git_tools import GitTools
from tests.features.test_hook_process import checker
from tests.features.test_memory_context import config_at
from tests.features.test_project_rule_execution import rule_text
from tests.features.test_project_rules import write_rule
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_hooks import hooked_app, model_for
from tests.features.test_tool_loop import answer, tool_response


def test_real_git_and_file_queries_overlap_without_changing_repository(tmp_path, monkeypatch):
    """只在查询入口加同步屏障；Git 命令、文件内容、结果编码和日志均真实执行。"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "core.hooksPath=", *args],
            env=env,
            check=True,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    git("init")
    target = tmp_path / "tracked.txt"
    target.write_bytes(b"before\n")
    git("add", "tracked.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "baseline")
    target.write_bytes(b"after\n")
    index_bytes = (tmp_path / ".git" / "index").read_bytes()
    head = git("rev-parse", "HEAD").stdout
    first_pair = Barrier(2)
    query, read, popen = GitTools._query, FileTools.read_file, subprocess.Popen
    workers, processes = [], []
    mutex = Lock()
    active = peak = 0

    def observe(action, wait):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
            workers.append(current_thread())
        try:
            if wait:
                first_pair.wait(timeout=6)
            return action()
        finally:
            with mutex:
                active -= 1

    def git_query(self, name, path, command):
        return observe(lambda: query(self, name, path, command), name == "git_status")

    def read_file(self, path, args):
        return observe(lambda: read(self, path, args), path == target)

    def start(argv, **kwargs):
        process = popen(argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(GitTools, "_query", git_query)
    monkeypatch.setattr(FileTools, "read_file", read_file)
    monkeypatch.setattr(subprocess, "Popen", start)
    calls = [
        call("status", "git_status", path="."),
        call("read", "read_file", path="tracked.txt"),
        call("diff", "git_diff", path="."),
        call("log", "git_log", path="."),
        call("missing", "read_file", path="missing.txt"),
        call("info", "file_info", path="tracked.txt"),
    ]
    bodies = []
    with hooked_app(tmp_path, model_for(calls, bodies), HooksConfig()) as (app, store):
        assert app.send("inspect the repository") == "done"
        sid = app.session.session_id
        results = outputs(bodies[-1])
        assert [r["ok"] for r in results] == [True, True, True, True, False, True]
        assert "tracked.txt" in results[0]["text"]
        assert results[1]["lines"] == [{"line": 1, "text": "after"}]
        assert "+after" in results[2]["text"] and "baseline" in results[3]["text"]
        assert results[5]["size"] == len(b"after\n")
        assert app.usage().tokens == 24 and not app.usage().unknown
        events = store.load_events(sid)
        for kind in ("tool_started", "tool_finished"):
            records = [e["payload"] for e in events if e["type"] == kind]
            assert [r["tool_call_id"] for r in records] == [c[0] for c in calls]
            assert len({r["turn_id"] for r in records}) == 1
            assert len({r["request_id"] for r in records}) == 1
    assert peak == 2 and active == 0
    assert workers and all(not worker.is_alive() for worker in workers)
    assert processes and all(process.poll() is not None for process in processes)
    assert (tmp_path / ".git" / "index").read_bytes() == index_bytes
    assert not (tmp_path / ".git" / "index.lock").exists()
    assert git("rev-parse", "HEAD").stdout == head and target.read_bytes() == b"after\n"
    with SessionStore(tmp_path / "sessions") as reopened:
        saved = [m.raw["tool_call_id"] for m in reopened.load_messages(sid) if m.role == "tool"]
        assert saved == [c[0] for c in calls]


@pytest.mark.parametrize("tool_limit", [6, 7])
def test_rules_hooks_mode_and_budget_keep_parallel_reads_within_boundaries(
    tmp_path, monkeypatch, tool_limit
):
    """新规则先阻止整批；预算允许后，实际读取并发，Hook 与模式分别拒绝其余动作。"""
    write_rule(tmp_path, "ROOT_RULE")
    write_rule(tmp_path / "src", "SOURCE_RULE")
    (tmp_path / "src" / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    (tmp_path / "blocked.txt").write_text("must not read")
    program = checker(
        tmp_path,
        "import json, sys\nfrom pathlib import Path\n"
        "data = json.load(sys.stdin)\n"
        "Path(data['event'] + '-' + data['source']['tool_call_id']).write_text('checked')\n"
        "if data['event'] == 'before_tool':\n"
        "    choice = 'deny' if data['tool']['arguments'].get('path') == 'blocked.txt' else 'allow'\n"
        "    print(json.dumps({'version': 1, 'decision': choice, 'reason': 'local rule'}))\n"
        "else:\n    print(json.dumps({'version': 1, 'note': 'observed'}))\n",
    )
    config = config_at(tmp_path)
    config = replace(
        config,
        agent=replace(config.agent, max_tool_calls=tool_limit),
        hooks=HooksConfig(enabled=True, before_tool=program, after_tool=program),
        reflection=ReflectionConfig(enabled=True),
    )
    requests, reads = [], []
    pair = Barrier(2)
    original = FileTools.read_file

    def read(self, path, args):
        reads.append(path.name)
        assert path.name != "blocked.txt"
        pair.wait(timeout=6)
        return original(self, path, args)

    monkeypatch.setattr(FileTools, "read_file", read)
    marker = tmp_path / "must-not-run"

    def batch(prefix):
        return [
            call(prefix + "a", "read_file", path="src/a.txt"),
            call(prefix + "b", "read_file", path="b.txt"),
            call(
                prefix + "command",
                "run_command",
                argv=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
            ),
        ]

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert "ROOT_RULE" in rule_text(body) and "SOURCE_RULE" not in rule_text(body)
            calls = batch("defer-")
        elif len(requests) == 2:
            assert "SOURCE_RULE" in rule_text(body)
            assert reads == [] and not list(tmp_path.glob("before_tool-*"))
            # plan 模式直接拒绝命令；两个读取则因新作用域尚未展示而等待。
            assert [r["status"] for r in outputs(body)] == ["deferred", "deferred", "mode_blocked"]
            calls = batch("run-")
            calls.insert(2, call("blocked", "read_file", path="blocked.txt"))
        else:
            assert len(requests) == 3
            assert [r["ok"] for r in outputs(body)[-4:]] == [True, True, False, False]
            assert [r["status"] for r in outputs(body)[-2:]] == ["hook_blocked", "mode_blocked"]
            return httpx.Response(200, json=answer("bounded results"))
        return httpx.Response(200, json=tool_response(calls))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            tmp_path,
            confirm_command=lambda _: pytest.fail("blocked command reached approval"),
        )
        app.select_mode("plan")
        try:
            if tool_limit == 6:
                with pytest.raises(BudgetError):
                    app.send("inspect")
            else:
                assert app.send("inspect") == "bounded results"
            sid = app.session.session_id
            events = store.load_events(sid)
            calls = [e for e in events if e["type"] == "tool_started"]
            assert len(calls) == (3 if tool_limit == 6 else 7)
            assert sorted(reads) == ([] if tool_limit == 6 else ["a.txt", "b.txt"])
            assert len(store.load_hooks(sid)) == (0 if tool_limit == 6 else 5)
            assert len(requests) == (2 if tool_limit == 6 else 3)
            assert not store.load_executions(sid) and not marker.exists()
            assert not store.load_reflections(sid)
            assert app.usage().tokens == len(requests) * 12
            assert events[-1]["payload"]["status"] == ("failed" if tool_limit == 6 else "succeeded")
        finally:
            app.close()
