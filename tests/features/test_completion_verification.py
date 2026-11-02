"""B38.2：虚假完成经过真实工具修正，验收与请求关联且修正次数有界。"""

import json
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.errors import AgentError, FailureCode
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.verification.files import FileStateVerifier
from tests.features.test_subagents import call
from tests.features.test_task_eval_runner import config
from tests.features.test_tool_loop import answer, tool_response


def test_false_final_is_repaired_with_existing_tools_and_logged_before_success(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    target = workspace / "result.txt"
    target.write_bytes(b"wrong")
    verifier = FileStateVerifier(workspace, {"result.txt": b"correct"})
    bodies, approvals = [], []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            payload = answer("已经完成")
        elif len(bodies) == 2:
            assert "独立验收未通过" in body["messages"][-1]["content"]
            payload = tool_response([call("read", "read_file", path="result.txt")])
        elif len(bodies) == 3:
            version = json.loads(body["messages"][-1]["content"])["version"]
            payload = tool_response(
                [
                    call(
                        "edit",
                        "replace_text",
                        path="result.txt",
                        old_text="wrong",
                        new_text="correct",
                        expected_version=version,
                    )
                ]
            )
        else:
            payload = answer("已修正")
        return httpx.Response(200, json=payload)

    cfg = replace(config(tmp_path), tool_discovery=ToolDiscoveryConfig(mode="full"))
    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            cfg,
            store,
            LLMClient(cfg.llm, http),
            workspace,
            verifier=verifier,
            confirm_edit=lambda plan: approvals.append(plan) is None,
        )
        try:
            assert app.send("修正result.txt") == "已修正"
            sid = app.session.session_id
            assert len(approvals) == 1 and target.read_bytes() == b"correct"
        finally:
            app.close()
    report = read_report(next((tmp_path / "sessions").glob(f"*/*/*/{sid}.jsonl")))
    turn = report["turns"][0]
    assert turn["status"] == "succeeded" and len(turn["requests"]) == 4
    assert [v["status"] for v in turn["verifications"]] == ["failed", "passed"]
    assert {v["request_id"] for v in turn["verifications"]} <= {
        r["request_id"] for r in turn["requests"]
    }


def test_only_two_corrections_and_no_success_history_on_persistent_failure(tmp_path):
    verifier = FileStateVerifier(tmp_path, {"missing.txt": b"correct"})
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=answer("成功"))

    cfg = config(tmp_path)
    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(cfg, store, LLMClient(cfg.llm, http), tmp_path, verifier=verifier)
        try:
            with pytest.raises(AgentError) as raised:
                app.send("完成任务")
            assert raised.value.code == FailureCode.VERIFICATION_FAILED
            assert len(calls) == 3
            assert store.load_messages(app.session.session_id) == []
        finally:
            app.close()
