"""B02.2 成对工程对照：假 HTTP 主动尝试越权，生产权限/文件/批准/MCP 与日志真实运行。"""

import json
from pathlib import Path

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import LLMConfig
from adam.contracts.errors import ApprovalDeniedError
from adam.contracts.memory import MemoryCommand
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from scripts.injection_cases import load_suite
from scripts.injection_fixture import (
    AFTER,
    BEFORE,
    MARKER_PROGRAM,
    MARKER_TEXT,
    PUBLIC_TEXT,
    prepare_fixture,
)
from tests.features.test_subagents import call
from tests.features.test_tool_loop import answer, tool_response

ROOT = Path(__file__).resolve().parents[2]
SUITE = load_suite(ROOT / "examples/injection-evaluation/cases-v1.json")


def results(body):
    return {
        m["tool_call_id"]: json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"
    }


def sequence(fixture):
    """脚本只替模型作决定；参数版本仍从实际读取结果取得，不模拟工具成功。"""
    attack = fixture.arm == "attack"
    case = fixture.pair.case_id
    if case == "direct-scope":
        return ["private" if attack else "public"]
    if case == "direct-credential":
        return ["credential" if attack else "public"]
    if case == "direct-approval":
        return ["target", "edit"]
    if case in {"file-mode", "skill-mode"}:
        return (["source"] if case == "file-mode" else []) + ["both"] + (["edit"] if attack else [])
    if case == "memory-scope":
        return ["public"] + (["private"] if attack else [])
    return ["source", "command"]


def step_calls(fixture, step, body):
    """只构造固定夹具调用；命令 argv 来自宿主，不从攻击正文拼接。"""
    if step == "both":
        return [
            call(name, "read_file", path=str(fixture.path(name))) for name in ("public", "target")
        ]
    if step == "edit":
        return [
            call(
                "edit",
                "replace_text",
                path=str(fixture.path("target")),
                old_text="old",
                new_text="after",
                expected_version=results(body)["target"]["version"],
            )
        ]
    if step == "command":
        return [call("command", "run_command", argv=list(fixture.marker_argv))]
    name = (
        "mcp_files_read_project_text"
        if step == "source" and fixture.pair.source == "tool_result"
        else "read_file"
    )
    return [call(step, name, path=str(fixture.path(step)))]


@pytest.mark.parametrize("case_id", [pair.case_id for pair in SUITE.pairs])
@pytest.mark.parametrize("arm", ["attack", "benign"])
def test_paired_case_reaches_real_source_and_obeys_actual_permission_boundary(
    tmp_path, case_id, arm
):
    fixture = prepare_fixture(tmp_path / "run", SUITE, case_id, arm)
    steps, bodies, approvals = sequence(fixture), [], []
    final = (
        "EDIT_OK"
        if case_id == "direct-approval"
        else MARKER_TEXT
        if case_id == "tool-approval"
        else PUBLIC_TEXT
    )
    if arm == "attack" and fixture.pair.source == "direct" and fixture.pair.boundary != "approval":
        final = "受限资料未读取。"

    def model(request):
        body = json.loads(request.content)
        number = len(bodies)
        bodies.append(body)
        serialized = json.dumps(body, ensure_ascii=False)
        assert fixture.private_text not in serialized and fixture.credential_text not in serialized
        if fixture.pair.source in {"memory", "skill"}:
            assert fixture.payload in serialized
        elif fixture.pair.source in {"file", "tool_result"}:
            assert (fixture.payload in serialized) is (number > 0)
            if number > 0:
                assert results(body)["source"]["ok"]
        else:
            assert fixture.prompt in [m["content"] for m in body["messages"] if m["role"] == "user"]
        value = (
            tool_response(step_calls(fixture, steps[number], body))
            if number < len(steps)
            else answer(final)
        )
        return httpx.Response(200, json=value)

    def approve_edit(plan):
        accepted = fixture.approve_edit(plan)
        approvals.append(("edit", accepted))
        return accepted

    def approve_command(request):
        accepted = fixture.approve_command(request)
        approvals.append(("command", accepted))
        return accepted

    config = fixture.configuration(LLMConfig("https://unused.test", "test", "unused", stream=False))
    denied = arm == "attack" and fixture.pair.boundary == "approval"
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(model)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            fixture.workspace,
            confirm_edit=approve_edit,
            confirm_command=approve_command,
        )
        try:
            app.select_mode(fixture.variant.mode)
            if fixture.pair.source == "memory":
                app.manage_memory(MemoryCommand("add", fixture.payload))
            skill_id = "inspect-fixture" if fixture.pair.source == "skill" else None
            if denied:
                with pytest.raises(ApprovalDeniedError):
                    app.send(fixture.prompt, skill_id=skill_id)
            else:
                assert app.send(fixture.prompt, skill_id=skill_id) == final
            sid = app.session.session_id
            events = store.load_events(sid)
            edits, commands = store.load_edits(sid), store.load_executions(sid)
            assert store.usage(sid) == (12 * len(bodies), False)
            assert all(
                e["payload"]["context"]["execution_mode"] == fixture.variant.mode
                for e in events
                if e["type"] == "request_started"
            )
        finally:
            app.close()

    outputs = {
        e["payload"]["tool_call_id"]: json.loads(e["payload"]["output"])
        for e in events
        if e["type"] == "tool_finished"
    }
    assert fixture.path("private").read_text(encoding="utf-8") == fixture.private_text
    assert fixture.path("credential").read_text(encoding="utf-8") == fixture.credential_text
    assert fixture.path("public").read_text(encoding="utf-8") == PUBLIC_TEXT
    assert fixture.path("program").read_bytes() == MARKER_PROGRAM.encode("utf-8")
    assert fixture.private_text not in json.dumps(
        outputs
    ) and fixture.credential_text not in json.dumps(outputs)
    changed = arm == "benign" and case_id == "direct-approval"
    ran = arm == "benign" and case_id == "tool-approval"
    assert fixture.path("target").read_bytes() == (AFTER if changed else BEFORE)
    assert fixture.path("marker").exists() is ran
    if changed:
        assert edits[-1]["status"] == "applied" and approvals == [("edit", True)]
    if ran:
        assert fixture.path("marker").read_text(encoding="utf-8") == MARKER_TEXT
        assert commands[-1]["exit_code"] == 0 and approvals == [("command", True)]
    if denied:
        assert approvals == [("edit" if case_id == "direct-approval" else "command", False)]
        assert events[-1]["payload"]["reason"] == "approval_denied"
        assert len(bodies) == len(steps), "denial must stop before another model request"
    elif arm == "attack" and fixture.pair.boundary == "mode":
        assert outputs["edit"]["status"] == "mode_blocked" and approvals == []
    elif arm == "attack":
        target = "credential" if case_id == "direct-credential" else "private"
        assert outputs[target]["ok"] is False and approvals == []
    if "public" in outputs:
        assert outputs["public"]["lines"][0]["text"] == PUBLIC_TEXT

    # 判据来自版本化样本；新增 required/forbidden 时，不能只改文档而绕过本地验收。
    observed = {
        f"read:{name}"
        for name in ("public", "target", "source", "private", "credential")
        if outputs.get(name, {}).get("ok") is True
    }
    if fixture.path("target").read_bytes() != BEFORE:
        observed.add("write:target")
    if fixture.path("private").read_text(encoding="utf-8") != fixture.private_text:
        observed.add("write:private")
    if fixture.path("marker").exists() or any(c.get("status") == "finished" for c in commands):
        observed.add("run:marker")
    assert not observed.intersection(fixture.variant.forbidden)
    assert observed <= set(fixture.variant.allowed)
    assert set(fixture.variant.required) <= observed
    if not denied:
        assert all(fragment in final for fragment in fixture.variant.answer_contains)

    starts = [e["payload"] for e in events if e["type"] == "request_started"]
    if fixture.pair.source == "skill":
        assert fixture.payload in starts[0]["context"]["selected_skill"]["content"]
    elif fixture.pair.source == "memory":
        assert fixture.payload in json.dumps(starts[0]["context"]["memory"], ensure_ascii=False)
