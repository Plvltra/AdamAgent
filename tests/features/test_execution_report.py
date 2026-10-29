"""终端执行事实来自真实文件与进程记录，模型自述不作为验证证据。"""

import json
import sys

import httpx
import pytest

from adam.config.settings import LLMConfig
from adam.contracts.errors import StorageError
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_streaming import DONE, event, sse
from tests.support import run_chat as chat


@pytest.fixture(params=[False, True], ids=["json", "stream"])
def scripted_chat(tmp_path, request):
    """只替换 HTTP；工具、确认、文件写入、子进程及 JSONL 使用真实实现。"""
    stream = request.param
    output, requests = [], []
    with SessionStore(tmp_path / "sessions") as store:

        def run(steps, approvals=()):
            inputs = iter(["do work", *approvals, "/exit"])

            def respond(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"data": []})
                body = json.loads(request.content)
                index = len(requests)
                requests.append(body)
                step = steps[index]
                if callable(step):
                    step = step(body)
                if step is None:
                    # 故意错误宣称通过，断言程序事实清单不会照抄该结论。
                    message = {"role": "assistant", "content": "测试已通过"}
                else:
                    name, args = step
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call{index}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                        ],
                    }
                if stream:
                    delta = dict(message)
                    if calls := delta.get("tool_calls"):
                        delta["tool_calls"] = [
                            {"index": index, **call} for index, call in enumerate(calls)
                        ]
                    return sse(
                        [
                            event(delta),
                            event(
                                reason="tool_calls" if step is not None else "stop",
                                usage={"prompt_tokens": 10, "completion_tokens": 2},
                            ),
                            DONE,
                        ]
                    )
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "tool_calls" if step is not None else "stop",
                                "message": message,
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    },
                )

            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                chat(
                    LLMClient(
                        LLMConfig("https://example.test", "test", "key", stream=stream), http
                    ),
                    "",
                    store,
                    read=lambda _: next(inputs),
                    write=output.append,
                    workspace=tmp_path,
                )
            return "\n".join(output)

        yield run, store, output, requests


@pytest.mark.parametrize(
    "mode,expected,request_count",
    [
        ("zero", "进程已结束；退出码 0", 2),
        ("nonzero", "进程已结束；退出码 7", 2),
        ("timeout", "超时，未正常完成", 2),
        ("denied", "已拒绝，未执行", 1),
        ("start_failed", "启动失败", 2),
        ("invalid", "未开始", 2),
    ],
)
def test_command_report_uses_real_status(scripted_chat, mode, expected, request_count):
    run, store, _, requests = scripted_chat
    args = {"argv": [sys.executable, "-c", "pass"]}
    if mode == "nonzero":
        args["argv"] = [sys.executable, "-c", "raise SystemExit(7)"]
    elif mode == "timeout":
        args.update(argv=[sys.executable, "-c", "import time; time.sleep(10)"], timeout_seconds=1)
    elif mode == "start_failed":
        args["argv"] = ["AdamNonexistentExecutable_82cbe0"]
    elif mode == "invalid":
        args["argv"] = []
    approvals = [] if mode == "invalid" else ["n" if mode == "denied" else "y"]
    text = run([("run_command", args), None], approvals)
    facts = text.split("执行记录>", 1)[1]
    assert expected in facts and "不自动等于测试通过" in facts
    assert "测试已通过" not in facts and len(requests) == request_count
    session_id = store.list_sessions()[0].session_id
    assert all("执行记录>" not in item.content for item in store.load_messages(session_id))
    if mode == "denied":
        assert "approval_denied" in facts


def edit_step(body):
    version = json.loads(body["messages"][-1]["content"])["version"]
    return (
        "replace_text",
        {
            "path": "source.txt",
            "old_text": "old",
            "new_text": "new",
            "expected_version": version,
        },
    )


@pytest.mark.parametrize("boundary", ["none", "edit_finished", "answer", "report"])
def test_file_report_distinguishes_applied_unknown_and_unreadable(
    tmp_path, scripted_chat, monkeypatch, boundary
):
    run, store, _, requests = scripted_chat
    target = tmp_path / "source.txt"
    target.write_bytes(b"old")
    original = store.append_edit_event

    def append_edit(session_id, kind, payload):
        if kind == "edit_finished":
            raise StorageError("injected result failure")
        original(session_id, kind, payload)

    def fail(*args, **kwargs):
        raise StorageError("injected disk failure")

    if boundary == "edit_finished":
        monkeypatch.setattr(store, "append_edit_event", append_edit)
    elif boundary == "answer":
        monkeypatch.setattr(store, "append_messages", fail)
    elif boundary == "report":
        monkeypatch.setattr(store, "execution_report", fail)
    text = run([("read_file", {"path": "source.txt"}), edit_step, None], ["y"])
    facts = text.split("执行记录>", 1)[1]
    assert target.read_bytes() == b"new"
    assert len(requests) == (2 if boundary == "edit_finished" else 3)
    if boundary == "edit_finished":
        assert "结果未知" in facts and "已修改" not in facts
        assert "edit_log_error" in facts
    elif boundary == "report":
        assert "日志无法读取" in facts and "实际结果无法确认" in facts
    else:
        assert "已修改" in facts and "版本" in facts
        assert "修改记录也不代表已验证" in facts
    if boundary == "answer":
        assert "Adam（未保存）>" in text and "answer_save_failed" in facts
    session_id = store.list_sessions()[0].session_id
    history = store.load_messages(session_id)
    assert bool(history) == (boundary in {"none", "report"})
    assert all("执行记录>" not in item.content for item in history)


def test_command_result_log_failure_is_unknown(scripted_chat, monkeypatch):
    run, store, _, requests = scripted_chat
    original = store.append_command_event

    def fail(session_id, kind, payload):
        # 实际进程结束后模拟磁盘故障；没有终态不能把退出结果展示为已知。
        if kind == "command_finished":
            raise StorageError("injected command result failure")
        original(session_id, kind, payload)

    monkeypatch.setattr(store, "append_command_event", fail)
    text = run([("run_command", {"argv": [sys.executable, "-c", "pass"]})], ["y"])
    facts = text.split("执行记录>", 1)[1]
    assert "结果未知" in facts and "退出码" not in facts
    assert "command_log_error" in facts and len(requests) == 1


def test_denied_edit_report_keeps_original_bytes(tmp_path, scripted_chat):
    run, _, _, requests = scripted_chat
    target = tmp_path / "source.txt"
    target.write_bytes(b"old")
    text = run([("read_file", {"path": "source.txt"}), edit_step], ["n"])
    facts = text.split("执行记录>", 1)[1]
    assert "已拒绝，未执行" in facts and "已修改" not in facts
    assert "approval_denied" in facts and len(requests) == 2
    assert target.read_bytes() == b"old"


def test_read_only_task_does_not_claim_verification(tmp_path, scripted_chat):
    run, _, _, _ = scripted_chat
    (tmp_path / "source.txt").write_bytes(b"old")
    text = run([("read_file", {"path": "source.txt"}), None])
    assert "本轮未记录文件修改或命令执行；未确认测试结果" in text


def test_plain_chat_does_not_print_execution_report(scripted_chat):
    run, _, _, _ = scripted_chat
    assert "执行记录>" not in run([None])
