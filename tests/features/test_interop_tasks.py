"""真实 HTTP 生命周期验收；延迟注入响应边界，不替换任务执行或状态。"""

import time
from threading import Thread

import pytest

from adam.interop.models import InteropError
from adam.interop.server import create_server
from adam.interop.task_service import TaskService
from adam.interop.tasks import TaskClient


@pytest.fixture
def remote():
    calls = []

    def work(text, cancel):
        calls.append(text)
        if text == "wait":
            cancel.wait(5)
        if text == "fail":
            raise ValueError("business failure")
        return text.upper()

    tasks = TaskService(work)
    server = create_server(str, tasks=tasks)
    thread = Thread(target=server.serve_forever)
    thread.start()
    try:
        yield TaskClient(f"http://127.0.0.1:{server.server_port}"), tasks, calls
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        tasks.close()


def terminal(client, local_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = client.query(local_id)
        if task.state in {"completed", "failed", "canceled"}:
            return task
        time.sleep(0.01)
    pytest.fail("task did not finish")


def test_query_completion_and_restore_same_remote_task(remote):
    client, _, calls = remote
    first = client.submit("local-1", "hello")
    assert first.remote_id and first.context_id
    restored = TaskClient(client.origin)
    restored.attach(first)
    done = terminal(restored, "local-1")
    assert done.remote_id == first.remote_id
    assert done.text == "HELLO"
    assert done.state == "completed"
    with pytest.raises(InteropError, match="已经派发"):
        restored.submit("local-1", "hello")
    assert calls == ["hello"]


def test_cooperative_cancellation_and_failure_terminal(remote):
    client, _, calls = remote
    task = client.submit("cancel", "wait")
    response = client.cancel("cancel")
    assert response.state in {"working", "canceled"}
    stopped = terminal(client, "cancel")
    assert stopped.state == "canceled" and stopped.remote_id == task.remote_id
    assert stopped.text == ""
    client.submit("fail", "fail")
    assert terminal(client, "fail").state == "failed"
    assert calls.count("wait") <= 1 and calls.count("fail") == 1


def test_query_timeout_then_reconnect_never_resubmits(remote, monkeypatch):
    client, tasks, calls = remote
    original = client.submit("once", "hello")
    get = tasks.get

    def delayed(task_id):
        time.sleep(0.15)
        return get(task_id)

    monkeypatch.setattr(tasks, "get", delayed)
    client.timeout = 0.04
    with pytest.raises(InteropError, match="可能未知"):
        client.query("once")
    assert client.tasks["once"].state == "unknown"
    assert client.tasks["once"].remote_id == original.remote_id
    monkeypatch.setattr(tasks, "get", get)
    client.timeout = 2
    assert terminal(client, "once").state == "completed"
    assert calls == ["hello"]


def test_initial_response_lost_blocks_duplicate_submission(remote, monkeypatch):
    client, tasks, calls = remote
    submit = tasks.submit

    def delayed(text):
        task = submit(text)
        time.sleep(0.15)
        return task

    monkeypatch.setattr(tasks, "submit", delayed)
    client.timeout = 0.04
    with pytest.raises(InteropError, match="可能未知"):
        client.submit("lost", "hello")
    assert client.tasks["lost"].remote_id is None
    restored = TaskClient(client.origin)
    restored.attach(client.tasks["lost"])
    with pytest.raises(InteropError, match="已经派发"):
        restored.submit("lost", "hello")
    with pytest.raises(InteropError, match="已经派发"):
        client.submit("lost", "hello")
    with pytest.raises(InteropError, match="ID 未知"):
        client.query("lost")
    assert calls == ["hello"]


def test_restore_rejects_other_service(remote):
    client, _, _ = remote
    task = client.submit("origin", "hello")
    with pytest.raises(InteropError, match="不能恢复"):
        TaskClient("http://127.0.0.1:1").attach(task)
