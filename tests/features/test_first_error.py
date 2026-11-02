"""B32.1：真实业务日志的首错定位与独立答案错误判定。"""

import json

from adam.application.conversation import Conversation
from adam.config.settings import AgentConfig
from adam.contracts.models import Completion
from adam.session.storage import SessionStore
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from scripts.first_error import analyze


def test_first_tool_error_is_evidence_not_automatically_root_cause(tmp_path):
    class Model:
        count = 0

        def complete(self, messages, **kwargs):
            self.count += 1
            if self.count == 1:
                return Completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "read",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"absent.txt"}',
                                },
                            }
                        ],
                    },
                    (1, 1),
                )
            return Completion({"role": "assistant", "content": "no file"}, (1, 1))

    with SessionStore(tmp_path / "sessions") as store:
        conversation = Conversation(
            store,
            Model(),
            "system",
            ToolExecutor(store, create_tool_registry(tmp_path), lambda _: None),
            AgentConfig(),
            model="test",
        )
        conversation.send("find file")
        sid = conversation.session.session_id
    path = next((tmp_path / "sessions").glob(f"*/*/*/{sid}.jsonl"))
    before = path.read_bytes()
    report = analyze(path)
    assert report["first_observed_error"]["event"] == "tool_finished"
    assert report["first_observed_error"]["tool_call_id"] == "read"
    assert report["primary"]["confidence"] == "medium" and report["root_cause"] == "not_proven"
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({"session_id": sid, "verification": {"passed": True}}))
    recovered = analyze(path, verdict)
    assert recovered["primary"]["category"] == "no_verified_task_failure"
    assert recovered["secondary"]
    assert path.read_bytes() == before


def test_successful_execution_can_still_have_wrong_answer(tmp_path):
    (tmp_path / "fact.txt").write_text("2+2=4", encoding="utf-8")

    class Model:
        calls = 0

        def complete(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fact",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"fact.txt"}',
                                },
                            }
                        ],
                    },
                    (1, 1),
                )
            assert json.loads(messages[-1]["content"])["ok"]
            return Completion({"role": "assistant", "content": "5"}, (1, 1))

    with SessionStore(tmp_path / "sessions") as store:
        conversation = Conversation(
            store,
            Model(),
            "system",
            ToolExecutor(store, create_tool_registry(tmp_path), lambda _: None),
            AgentConfig(),
            model="test",
        )
        assert conversation.send("2+2?") == "5"
        sid = conversation.session.session_id
    path = next((tmp_path / "sessions").glob(f"*/*/*/{sid}.jsonl"))
    assert analyze(path)["primary"]["category"] == "insufficient_evidence"
    verdict = tmp_path / "verdict.json"
    verdict.write_text(
        json.dumps(
            {"session_id": sid, "verification": {"passed": False, "reasons": ["expected 4"]}}
        )
    )
    report = analyze(path, verdict)
    assert report["first_observed_error"] is None
    assert report["primary"]["category"] == "final_verification_failed"
    assert report["primary"]["confidence"] == "high"
