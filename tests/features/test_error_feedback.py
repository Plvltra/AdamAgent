"""失败反馈业务：统一展示、预算分类、真实 IO 故障与意外错误传播。"""

import json
import os
import sys

import httpx
import pytest

import adam.cli as cli
from adam.config import AgentConfig, LLMConfig
from adam.conversation import Conversation
from adam.errors import AgentError
from adam.llm import LLMClient
from adam.storage import SessionStore, StorageError


def make_client(requests, *, fail_request=False):
    """只替换远端 HTTP；默认返回固定回答及可靠用量。"""

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        requests.append(json.loads(request.content))
        if fail_request:
            return httpx.Response(503, text="private-server-detail")
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("partial_reply", [None, "future answer"])
def test_new_business_error_needs_no_cli_branch(tmp_path, monkeypatch, partial_reply):
    """一个 CLI 从未导入的新错误类别也能展示提示及可选回答，并继续接收退出指令。"""

    class FutureFeatureError(AgentError):
        pass

    def fail_send(self, text):
        raise FutureFeatureError("新业务暂不可用", partial_reply=partial_reply)

    monkeypatch.setattr(Conversation, "send", fail_send)
    requests = []
    output = []
    inputs = iter(["question", "/exit"])
    with make_client(requests) as http, SessionStore(tmp_path / "sessions") as store:
        cli.chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=output.append,
            workspace=tmp_path,
        )
    assert output.count("新业务暂不可用") == 1
    assert any("未保存" in line for line in output) is (partial_reply is not None)
    if partial_reply is not None:
        assert any(partial_reply in line for line in output)
    assert output[-1] == "再见。"
    assert requests == []


def test_budget_stop_is_not_reported_as_model_failure(tmp_path):
    """首轮允许完成，下一轮超额时不发送请求，终端按预算停止展示。"""
    requests = []
    output = []
    inputs = iter(["first", "blocked", "/exit"])
    with make_client(requests) as http, SessionStore(tmp_path / "sessions") as store:
        cli.chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=output.append,
            workspace=tmp_path,
            agent_config=AgentConfig(max_session_tokens=1),
        )
        session_id = store.list_sessions()[0].session_id
        assert [message.content for message in store.load_messages(session_id)] == ["first", "done"]
        assert store.usage(session_id) == (12, False)
        events = store.load_events(session_id)
        assert events[-1]["type"] == "turn_finished"
        assert events[-1]["payload"]["status"] == "failed"
        assert len([e for e in events if e["type"] == "request_started"]) == 1
    assert len(requests) == 1
    assert any("执行受限" in line and "额度" in line for line in output)
    assert not any("模型请求错误" in line for line in output)


def test_api_failure_is_reported_once_without_remote_body(tmp_path):
    """HTTP 失败仍记录未知用量；统一展示不重复添加类别，也不显示远端正文。"""
    requests = []
    output = []
    inputs = iter(["question", "/exit"])
    with (
        make_client(requests, fail_request=True) as http,
        SessionStore(tmp_path / "sessions") as store,
    ):
        cli.chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            read=lambda _: next(inputs),
            write=output.append,
            workspace=tmp_path,
        )
        session_id = store.list_sessions()[0].session_id
        assert store.usage(session_id) == (0, True)
        assert store.load_messages(session_id) == []
    assert len(requests) == 1
    assert "\n".join(output).count("模型请求错误") == 1
    assert any("503" in line for line in output)
    assert "private-server-detail" not in "\n".join(output)


def test_final_answer_disk_failure_is_visible_without_retry(tmp_path, monkeypatch):
    """在实际消息提交的 fsync 边界注入失败，验证回滚、回答保留与统一终端提示。"""
    root = tmp_path / "sessions"
    requests = []
    output = []
    with make_client(requests) as http, SessionStore(root) as store:
        session_id = store.create_session("test", "")
        path = next(root.glob("*/*/*/*.jsonl"))
        real_fsync = os.fsync
        failures = []

        def fail_commit_once(fd):
            last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
            if last["type"] == "turn_completed" and not failures:
                failures.append(True)
                raise OSError("private-disk-detail")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_commit_once)
        inputs = iter(["question", "/exit"])
        cli.chat(
            LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
            "",
            store,
            resume_id=session_id,
            read=lambda _: next(inputs),
            write=output.append,
            workspace=tmp_path,
        )
    assert failures == [True] and len(requests) == 1
    assert any("未保存" in line and "done" in line for line in output)
    assert any("本轮保存失败" in line for line in output)
    assert "private-disk-detail" not in "\n".join(output)
    with SessionStore(root) as store:
        assert store.load_messages(session_id) == []
        assert store.usage(session_id) == (12, False)
        events = store.load_events(session_id)
        assert not any(event["type"] == "turn_completed" for event in events)
        request = next(e["payload"] for e in events if e["type"] == "request_finished")
        assert request["status"] == "succeeded"
        assert events[-1]["type"] == "turn_finished"
        assert events[-1]["payload"]["status"] == "failed"


@pytest.mark.parametrize("phase", ["initial", "answer"])
@pytest.mark.parametrize("failure", [OSError("terminal failed"), TypeError("display bug")])
def test_unexpected_output_error_propagates_and_releases_session(tmp_path, failure, phase):
    """初始界面或回答展示失败均释放会话锁；错误原样传播，已提交回答保留。"""
    root = tmp_path / "sessions"
    output = []
    requests = []

    def write(line):
        if (phase == "initial" and line.startswith("Adam —")) or (
            phase == "answer" and line.endswith("done")
        ):
            raise failure
        output.append(line)

    with make_client(requests) as http, SessionStore(root) as store:
        session_id = store.create_session("test", "")
        with pytest.raises(type(failure)) as caught:
            cli.chat(
                LLMClient(LLMConfig("https://example.test", "test", "secret"), http),
                "",
                store,
                resume_id=session_id,
                read=lambda _: "question",
                write=write,
                workspace=tmp_path,
            )
        assert caught.value is failure
        with SessionStore(root) as other:
            other.select_session(session_id)
            messages = [message.content for message in other.load_messages(session_id)]
            assert messages == (["question", "done"] if phase == "answer" else [])
    assert len(requests) == (1 if phase == "answer" else 0)
    assert not any("会话文件操作失败" in line for line in output)


def test_read_failure_has_public_message_and_preserves_cause(tmp_path, monkeypatch):
    """模拟读取会话文件的系统故障；存储边界保留原因链但不公开原始详情。"""
    with SessionStore(tmp_path / "sessions") as store:
        session_id = store.create_session("test", "")
        original_read = type(store.root).read_bytes
        failure = OSError("private-read-detail")

        def fail_read(path):
            if path.suffix == ".jsonl":
                raise failure
            return original_read(path)

        monkeypatch.setattr(type(store.root), "read_bytes", fail_read)
        with pytest.raises(StorageError) as caught:
            store.load_messages(session_id)
        assert isinstance(caught.value, AgentError)
        assert caught.value.__cause__ is failure
        assert "private-read-detail" not in caught.value.user_message


def test_storage_programming_error_is_not_converted(tmp_path, monkeypatch):
    """写入阶段的意外类型错误仍向上传播，回滚后文件保持原样。"""
    with SessionStore(tmp_path / "sessions") as store:
        session_id = store.create_session("test", "")
        path = next(store.root.glob("*/*/*/*.jsonl"))
        original = path.read_bytes()
        real_fsync = os.fsync
        calls = []

        def fail_once(fd):
            calls.append(fd)
            if len(calls) == 1:
                raise TypeError("storage bug")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_once)
        with pytest.raises(TypeError, match="storage bug"):
            store.start_turn(session_id, "question")
        assert path.read_bytes() == original


@pytest.mark.parametrize("failure", ["session_directory", "workspace"])
def test_startup_failure_uses_public_error_contract(tmp_path, monkeypatch, capsys, failure):
    """真实启动入口统一展示存储创建或工具初始化失败；失败发生前不请求远端模型。"""
    config = tmp_path / "config.toml"
    config.write_text('[chat]\nsystem_prompt=""\n', encoding="utf-8")
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://example.test"\nmodel="test"\napi_key="secret"\n',
        encoding="utf-8",
    )
    workspace = tmp_path
    if failure == "session_directory":
        (tmp_path / "sessions").write_text("not a directory", encoding="utf-8")
    else:
        workspace = tmp_path / "missing"

    client_type = httpx.Client
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **_: client_type(
            transport=httpx.MockTransport(lambda _: pytest.fail("Must not request the model"))
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["adam", "--config", str(config), "--workspace", str(workspace)]
    )
    assert cli.main() == 2
    output = capsys.readouterr().out
    expected = "会话文件操作失败" if failure == "session_directory" else "工作目录不存在"
    assert expected in output and "secret" not in output
