"""开发诊断读取真实日志，展示失败证据但不请求模型或修改会话。"""

import json
import subprocess
import sys

import httpx
import pytest

from adam.config import LLMConfig
from adam.diagnostics import main, read_report
from adam.llm import LLMClient, LLMError
from adam.storage import SessionStore
from adam.tool_calls.factory import create_tool_registry
from tests.support import build_conversation


def test_failed_turn_report_is_read_only(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "inspect",
                                    "type": "function",
                                    "function": {"name": "file_info", "arguments": '{"path":"."}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
    ):
        conversation = build_conversation(
            store,
            LLMClient(LLMConfig("https://example.test", "test", "secret", stream=False), http),
            "",
            create_tool_registry(tmp_path),
        )
        with pytest.raises(LLMError):
            conversation.send("inspect then explain")
        path = next((tmp_path / "sessions").glob("*/*/*/*.jsonl"))
        before = path.read_bytes()
        report = read_report(path)  # 即使写锁被当前会话持有，诊断也只读快照。
        turn = report["turns"][0]
        assert turn["user_text"] == "inspect then explain"
        assert turn["status"] == "failed" and turn["reason"] == "api_http"
        assert turn["error"]["http_status"] == 503 and turn["duration_ms"] >= 0
        assert [r["status"] for r in turn["requests"]] == ["succeeded", "failed"]
        assert turn["requests"][0]["usage"] == {"input_tokens": 10, "output_tokens": 2}
        assert turn["tools"][0]["request_id"] == turn["requests"][0]["request_id"]
        assert turn["tools"][0]["finished"] and turn["tools"][0]["output"]
        assert report["known_tokens"] == 12 and report["unknown_usage"]
        assert path.read_bytes() == before
        assert store.load_messages(conversation.session.session_id) == []
    assert len(calls) == 2


def test_module_entry_needs_only_a_log_file(tmp_path):
    with SessionStore(tmp_path / "s") as store:
        session_id = store.create_session("诊断", "")
        store.start_turn(session_id, "尚未请求")
    path = next((tmp_path / "s").glob("*/*/*/*.jsonl"))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = subprocess.run(
        [sys.executable, "-m", "adam.diagnostics", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PYTHONIOENCODING": "utf-8"},
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    turn = json.loads(result.stdout)["turns"][0]
    assert turn["status"] == "in_progress" and turn["requests"] == []
    assert turn["reason"] is None and turn["duration_ms"] is None
    assert {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("content", [None, b'{"broken":', b"{}\n"])
def test_invalid_log_diagnosis_leaves_source_unchanged(tmp_path, capsys, content):
    path = tmp_path / "12345678.jsonl"
    if content is not None:
        path.write_bytes(content)
    assert main([str(path)]) == 1
    captured = capsys.readouterr()
    assert not captured.out and captured.err
    assert path.read_bytes() == content if content is not None else not path.exists()
