"""通用工具完成测试、修改、重测；编辑额度限制实际尝试，不解释模型的修复意图。"""

import hashlib
import json
import sys

import httpx
import pytest

from adam.config import AgentConfig, LLMConfig
from adam.diagnostics import read_report
from adam.errors import ApprovalDeniedError, BudgetError
from adam.llm import LLMClient
from adam.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def tool(name, args, call_id):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def response(calls=None):
    message = (
        {"role": "assistant", "content": None, "tool_calls": calls}
        if calls
        else {"role": "assistant", "content": "工作结束"}
    )
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": message,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )


@pytest.mark.parametrize("approve", [True, False])
def test_failed_check_edit_and_recheck(tmp_path, approve):
    """模型收到真实断言失败，再提出修改；拒绝则不重测，也不请求总结。"""
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "check.py").write_text(
        "from calc import add\nassert add(2, 3) == 5\nprint('verified addition')\n",
        encoding="utf-8",
    )
    requests, results, approvals = [], [], []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        step = len(requests)
        if step > 1:
            results.append(json.loads(body["messages"][-1]["content"]))
        if step == 1:
            calls = [tool("read_file", {"path": "calc.py"}, "read")]
        elif step in {2, 4}:
            # -B 避免同秒、同长度改动复用 Python 的字节码缓存，验证真实源文件。
            calls = [
                tool("run_command", {"argv": [sys.executable, "-B", "check.py"]}, f"check{step}")
            ]
        elif step == 3:
            assert results[-1]["exit_code"] != 0 and "AssertionError" in results[-1]["stderr"]
            calls = [
                tool(
                    "replace_text",
                    {
                        "path": "calc.py",
                        "old_text": "return a - b",
                        "new_text": "return a + b",
                        "expected_version": results[0]["version"],
                    },
                    "edit",
                )
            ]
        else:
            assert results[-1]["exit_code"] == 0
            assert "verified addition" in results[-1]["stdout"]
            return response()
        return response(calls)

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
            "",
            create_tool_registry(
                tmp_path,
                confirm_command=lambda _: True,
                confirm_edit=lambda plan: approvals.append(plan) or approve,
            ),
        )
        if approve:
            assert conversation.send("修复加法，并运行项目检查") == "工作结束"
        else:
            with pytest.raises(ApprovalDeniedError):
                conversation.send("修复加法，并运行项目检查")
        turn = read_report(next(store.root.rglob("*.jsonl")))["turns"][0]
        assert turn["status"] == ("succeeded" if approve else "failed")
        facts = conversation.execution_report()
        assert facts is not None
        assert [item.kind for item in facts.actions] == (
            ["run_command", "replace_text", "run_command"]
            if approve
            else ["run_command", "replace_text"]
        )
        assert facts.actions[0].exit_code != 0
        assert facts.actions[1].status == ("applied" if approve else "denied")
        if approve:
            assert facts.actions[2].exit_code == 0
        assert len(requests) == (5 if approve else 3)
        assert len(approvals) == 1
        assert ("return a + b" in source.read_text()) == approve
        commands = store.load_executions(conversation.session.session_id)
        assert len(commands) == (2 if approve else 1)
        assert commands[0]["exit_code"] != 0
        if approve:
            assert commands[1]["exit_code"] == 0
        assert store.load_edits(conversation.session.session_id)[0]["status"] == (
            "applied" if approve else "denied"
        )


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("batch", [True, False])
def test_edit_attempt_limit_stops_before_fourth_and_resets_next_turn(tmp_path, valid, batch):
    """失败参数同样占额度；跨请求与同批均在第四次前停止，新输入重新计数。"""
    source = tmp_path / "counter.txt"
    source.write_bytes(b"0")
    requests, approvals = [], []

    def edit_call(index):
        before = str(index) if valid else "absent"
        actual = str(index) if valid else "0"
        return tool(
            "replace_text",
            {
                "path": "counter.txt",
                "old_text": before,
                "new_text": str(index + 1),
                "expected_version": hashlib.sha256(actual.encode()).hexdigest(),
            },
            f"edit{index}",
        )

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        user = next(
            item["content"] for item in reversed(body["messages"]) if item["role"] == "user"
        )
        if user == "next":
            if body["messages"][-1]["role"] == "tool":
                return response()
            return response([edit_call(3 if valid else 0)])
        if batch:
            return response([edit_call(index) for index in range(4)])
        return response([edit_call(len(requests) - 1)])

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "key", stream=False), http),
            "",
            create_tool_registry(
                tmp_path, confirm_edit=lambda plan: approvals.append(plan) or True
            ),
            AgentConfig(max_edit_attempts=3),
        )
        with pytest.raises(BudgetError) as caught:
            conversation.send("repeat")
        assert caught.value.code == "edit_limit"
        facts = conversation.execution_report()
        assert facts is not None and facts.reason == "edit_limit"
        assert len(facts.actions) == 3
        assert len(requests) == (1 if batch else 4)
        turn = read_report(next(store.root.rglob("*.jsonl")))["turns"][0]
        assert turn["reason"] == "edit_limit" and len(turn["tools"]) == 3
        assert source.read_bytes() == (b"3" if valid else b"0")
        assert len(approvals) == (3 if valid else 0)
        assert conversation.send("next") == "工作结束"
        turns = read_report(next(store.root.rglob("*.jsonl")))["turns"]
        assert len(turns[1]["tools"]) == 1
        assert source.read_bytes() == (b"4" if valid else b"0")
        conversation.new()
        assert conversation.execution_report() is None
