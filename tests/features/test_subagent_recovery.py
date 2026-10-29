"""真实 TCP、子线程与独立进程故障验收；不访问外部 API 或用户会话。"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.features.test_cancellation import frame
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response


@pytest.fixture
def delegation_api():
    """服务端可停在响应头、正文、已解析用量之后，或让子回答完整提交。"""
    state = SimpleNamespace(controls={}, calls=[], ready={}, releases={}, parent_counts={})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            body = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def start_stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

        def send_result(self, data):
            self.start_stream()
            message = dict(data["choices"][0]["message"])
            if message.get("tool_calls"):
                message["tool_calls"] = [
                    {**item, "index": index} for index, item in enumerate(message["tool_calls"])
                ]
            self.wfile.write(frame(message))
            self.wfile.write(frame(reason=data["choices"][0]["finish_reason"], usage=data["usage"]))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append(body)
            user = next(item["content"] for item in body["messages"] if item["role"] == "user")
            child = "辅助子 Agent" in body["messages"][0]["content"]
            try:
                if child:
                    self.child_response(user)
                else:
                    self.parent_response(user.split()[-1], body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # 被取消或强杀的客户端关闭真实 TCP，是预期结果。

        def child_response(self, task):
            tag, name = task.split("|")
            control = state.controls[tag]
            ready, release = state.ready[(tag, name)], state.releases[(tag, name)]
            phase = control["phase"]
            if control.get("single"):
                phase = "known" if name == "a" else "body"
            if phase == "complete":
                self.send_result(answer("child " + name))
                ready.set()
                return
            if phase == "headers":
                ready.set()
                release.wait(30)
                self.send_result(answer("child " + name))
                return
            self.start_stream()
            self.wfile.write(frame({"content": "child " + name}))
            if phase == "known":
                self.wfile.write(frame(reason="stop", usage=answer()["usage"]))
            self.wfile.flush()
            ready.set()
            release.wait(30)
            if phase != "known":
                self.wfile.write(frame(reason="stop", usage=answer()["usage"]))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def parent_response(self, tag, body):
            state.parent_counts[tag] = state.parent_counts.get(tag, 0) + 1
            count = state.parent_counts[tag]
            if count == 1:
                result = tool_response(
                    [
                        call("a", "spawn_agent", name="A", task=tag + "|a"),
                        call("b", "spawn_agent", name="B", task=tag + "|b"),
                    ]
                )
            elif count == 2:
                calls = []
                if state.controls[tag].get("single"):
                    usage_path = state.controls[tag]["root"] / "usage-a"
                    deadline = time.monotonic() + 10
                    while not usage_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert usage_path.exists()
                    spawned = [
                        json.loads(item["content"])
                        for item in body["messages"]
                        if item["role"] == "tool"
                    ]
                    calls.append(call("stop", "interrupt_agent", agent_id=spawned[0]["agent_id"]))
                calls.append(call("wait", "wait_agents", agent_ids=[]))
                result = tool_response(calls)
            else:
                result = answer("summary " + tag)
            self.send_result(result)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        for event in state.releases.values():
            event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def prepare(api, root, tag, phase, *, single=False, sessions=None):
    root.mkdir(parents=True, exist_ok=True)
    api.controls[tag] = {"phase": phase, "single": single, "root": root}
    for name in ("a", "b"):
        api.ready[(tag, name)] = threading.Event()
        api.releases[(tag, name)] = threading.Event()
    sessions = sessions or root / "sessions"
    (root / "config.toml").write_text(
        "[storage]\nsessions_dir="
        + json.dumps(str(sessions))
        + "\n[agent]\nmax_session_tokens=200000\n",
        encoding="utf-8",
    )
    (root / "llm.toml").write_text(
        f'[llm]\nbase_url="{api.url}"\nmodel="test"\napi_key="local-key"\ntimeout_seconds=25\n',
        encoding="utf-8",
    )


def launch(root, mode, tag):
    project = Path(__file__).resolve().parents[2]
    worker = Path(__file__).parent / "support" / "subagent_worker.py"
    return subprocess.Popen(
        [sys.executable, str(worker), str(root), mode, tag],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def wait_for(process, condition, message):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if condition():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=3)
            pytest.fail(f"{message}: process exited: {stdout}\n{stderr}")
        time.sleep(0.02)
    pytest.fail(message)


def finish(process):
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert result["live_children"] == []
    assert result["tokens"] == sum(report["known_tokens"] for report in result["reports"])
    return result


def cleanup(process):
    if process is not None and process.poll() is None:
        process.kill()
        process.communicate(timeout=3)


def read_index(root, sessions=None):
    session_id = (root / "session-id").read_text(encoding="utf-8")
    return json.loads(
        ((sessions or root / "sessions") / "_subagents" / session_id / "index.json").read_text(
            encoding="utf-8"
        )
    )


def test_real_single_child_cancel_preserves_sibling_and_releases_threads(tmp_path, delegation_api):
    api = delegation_api
    prepare(api, tmp_path, "main", "body", single=True)
    process = launch(tmp_path, "run", "main")
    try:

        def stopped_only_a():
            if not (tmp_path / "session-id").exists():
                return False
            index_path = (
                tmp_path
                / "sessions"
                / "_subagents"
                / (tmp_path / "session-id").read_text()
                / "index.json"
            )
            if not index_path.exists():
                return False
            agents = read_index(tmp_path)["agents"]
            states = {
                agent["name"]: agent["runs"][-1]["status"] for agent in agents if agent["runs"]
            }
            return states.get("A") == "interrupted" and states.get("B") == "running"

        wait_for(process, stopped_only_a, "single cancellation did not leave B running")
        api.releases[("main", "b")].set()
        result = finish(process)
        states = {agent["name"]: agent["runs"][-1]["status"] for agent in result["index"]["agents"]}
        assert states == {"A": "interrupted", "B": "completed"}
        assert not result["unknown"]
        assert result["history"][-1] == "summary main"
        assert any(
            report["turns"][0]["status"] == "interrupted" for report in result["reports"][1:]
        )
    finally:
        cleanup(process)


@pytest.mark.parametrize("phase", ["headers", "body", "known"])
def test_group_ctrl_c_stops_real_child_io_but_not_other_terminal(tmp_path, delegation_api, phase):
    api = delegation_api
    primary, other = tmp_path / "primary", tmp_path / "other"
    sessions = tmp_path / "shared-sessions"
    prepare(api, primary, "main", phase, sessions=sessions)
    process, other_process = launch(primary, "cancel", "main"), None
    try:
        wait_for(
            process,
            lambda: (
                all(api.ready[("main", name)].is_set() for name in ("a", "b"))
                and (primary / "parent-waiting").exists()
            ),
            "children did not enter real HTTP wait",
        )
        if phase == "known":
            wait_for(
                process,
                lambda: (primary / "usage-a").exists() and (primary / "usage-b").exists(),
                "client did not parse child usage",
            )
        if phase == "body":
            prepare(api, other, "other", "body", sessions=sessions)
            other_process = launch(other, "run", "other")
            wait_for(
                other_process,
                lambda: all(api.ready[("other", name)].is_set() for name in ("a", "b")),
                "other terminal did not start",
            )
        (primary / "cancel").touch()
        result = finish(process)
        assert {agent["runs"][-1]["status"] for agent in result["index"]["agents"]} == {
            "interrupted"
        }
        assert result["unknown"] is (phase != "known")
        assert result["history"] == []
        assert "已取消本轮" in "\n".join(result["output"])
        if other_process is not None:
            assert other_process.poll() is None
            assert all(
                agent["runs"][-1]["status"] == "running"
                for agent in read_index(other, sessions)["agents"]
            )
            for name in ("a", "b"):
                api.releases[("other", name)].set()
            other_result = finish(other_process)
            assert not other_result["unknown"]
            assert other_result["history"][-1] == "summary other"
    finally:
        cleanup(process)
        cleanup(other_process)


@pytest.mark.parametrize("phase", ["request", "committed"])
def test_process_kill_recovers_child_logs_without_reissuing(tmp_path, delegation_api, phase):
    api = delegation_api
    prepare(api, tmp_path, "main", "headers" if phase == "request" else "complete")
    process = launch(tmp_path, phase, "main")
    try:
        wait_for(
            process,
            lambda: (
                (tmp_path / "parent-waiting").exists()
                and (
                    all(api.ready[("main", name)].is_set() for name in ("a", "b"))
                    if phase == "request"
                    else len(list(tmp_path.glob("committed-*"))) == 2
                )
            ),
            "worker did not reach crash boundary",
        )
        process.kill()
        process.communicate(timeout=5)
        before = len(api.calls)
        restarted = launch(tmp_path, "resume", "main")
        try:
            result = finish(restarted)
        finally:
            cleanup(restarted)
        assert len(api.calls) == before
        assert result["history"] == []
        states = {agent["runs"][-1]["status"] for agent in result["index"]["agents"]}
        assert states == ({"interrupted"} if phase == "request" else {"completed"})
        assert result["unknown"] is (phase == "request")
        child_reports = result["reports"][1:]
        assert all(
            report["turns"][0]["status"] == ("interrupted" if phase == "request" else "succeeded")
            for report in child_reports
        )
        if phase == "committed":
            assert {agent["runs"][-1]["reply"] for agent in result["index"]["agents"]} == {
                "child a",
                "child b",
            }
    finally:
        cleanup(process)
