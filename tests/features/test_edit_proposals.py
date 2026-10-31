"""T11.1：真实文件、工具来源和 JSONL 的候选提交验收，尚不注册生产模型工具。"""

import hashlib
import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from adam.application.conversation import Conversation
from adam.application.proposals import ProposalSubmission
from adam.config.settings import AgentConfig, LLMConfig, PermissionConfig, ToolLimits
from adam.contracts.errors import RequestInterrupted, StorageError, ToolError
from adam.contracts.execution import ToolBudget, ToolCapability
from adam.contracts.proposals import ProposalOwner
from adam.llm.client import LLMClient
from adam.session.outcome import ExecutionOutcome
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@contextmanager
def candidate_at(root, *, raw=b"value = 1\n", read_args=None, tool_limits=None):
    """API 级夹具登记已结算请求；实际 read_file 由生产执行器读取并保存。"""
    target = root / "target.txt"
    target.write_bytes(raw)
    with SessionStore(root / "sessions") as store:
        sid = store.create_session("child", "")
        owner = ProposalOwner("aaaaaaaa", "b" * 32, sid, "c" * 16)
        turn = store.start_turn(sid, "propose an edit")
        request = store.start_request(sid, turn, "offline")
        store.finish_request(sid, turn, request, (10, 1))
        registry = create_tool_registry(root, limits=tool_limits)
        calls = tool_response([call("read", "read_file", path="target.txt", **(read_args or {}))])[
            "choices"
        ][0]["message"]["tool_calls"]
        ToolExecutor(store, registry, lambda _: None).execute(
            sid, turn, request, calls, ToolBudget(3)
        )
        service = ProposalSubmission(
            store, owner, PathPolicy(root, PermissionConfig()), ToolLimits()
        )
        yield SimpleNamespace(
            root=root,
            target=target,
            store=store,
            sid=sid,
            turn=turn,
            owner=owner,
            service=service,
            read_request=request,
        )


def pending_submit(env, *, identifier="submit", **changes):
    args = {
        "read_call_id": "read",
        "old_text": "value = 1",
        "new_text": "value = 2",
        "reason": "correct value",
        **changes,
    }
    request = env.store.start_request(env.sid, env.turn, "offline")
    env.store.finish_request(env.sid, env.turn, request, (10, 1))
    source = {"turn_id": env.turn, "request_id": request, "tool_call_id": identifier}
    env.store.append_tool_event(
        env.sid,
        "tool_started",
        {
            **source,
            "name": "submit_proposal",
            "arguments": json.dumps(args),
        },
    )
    env.service.bind_source(source)
    return source, args


def finish_submit(env, source, output):
    env.store.append_tool_event(
        env.sid,
        "tool_finished",
        {
            **source,
            "output": json.dumps(output),
        },
    )


def test_candidate_is_durable_read_only_and_recovery_does_not_apply(tmp_path):
    with candidate_at(tmp_path) as env:
        source, args = pending_submit(env)
        before = env.target.read_bytes()
        proposal = env.service.submit(**args)
        assert env.target.read_bytes() == before
        assert proposal.read.path == str(env.target)
        assert proposal.read.version == hashlib.sha256(before).hexdigest()
        assert proposal.target_version == hashlib.sha256(b"value = 2\n").hexdigest()
        assert proposal.read.request_id == env.read_request
        assert proposal.turn_id == env.turn and proposal.owner == env.owner
        assert env.store.load_proposals(env.sid) == [proposal]
        assert env.store.load_proposals(env.sid, "f" * 32) == []
        assert env.store.load_edits(env.sid) == env.store.load_executions(env.sid) == []
        sid = env.sid
        # 模拟提交候选后、工具结果保存前退出；恢复只中断执行，不抹掉已保存候选。
    with SessionStore(tmp_path / "sessions") as store:
        store.select_session(sid)
        assert store.load_proposals(sid) == [proposal]
        assert store.load_messages(sid) == []
        assert store.load_events(sid)[-1]["type"] == "session_recovered"
    assert (tmp_path / "target.txt").read_bytes() == b"value = 1\n"


@pytest.mark.parametrize(
    "raw,read_args,old,new,expected",
    [
        (
            b"first\nvalue = 1\nlast\n",
            {"start_line": 2, "max_lines": 1},
            "value = 1",
            "value = 2",
            b"first\nvalue = 2\nlast\n",
        ),
        (
            b"\xef\xbb\xbfvalue = 1\r\nnext\r\n",
            {},
            "value = 1\r\nnext",
            "value = 2\r\nnext",
            b"\xef\xbb\xbfvalue = 2\r\nnext\r\n",
        ),
        (b"value = 1", {}, "value = 1", "", b""),
    ],
)
def test_observed_partial_lines_bom_crlf_and_deletion_are_valid_candidates(
    tmp_path, raw, read_args, old, new, expected
):
    with candidate_at(tmp_path, raw=raw, read_args=read_args) as env:
        source, args = pending_submit(env, old_text=old, new_text=new)
        proposal = env.service.submit(**args)
        assert proposal.target_version == hashlib.sha256(expected).hexdigest()
        assert "\r" not in proposal.edit.old_text and "\r" not in proposal.edit.new_text
        assert env.target.read_bytes() == raw
        finish_submit(env, source, {"ok": True, "proposal_id": proposal.proposal_id})


@pytest.mark.parametrize(
    "case", ["invented", "other_tool", "previous_turn", "failed_read", "unfinished_read"]
)
def test_only_finished_same_turn_read_file_can_supply_evidence(tmp_path, case):
    with candidate_at(tmp_path) as env:
        read_id = "read"
        if case == "invented":
            read_id = "invented"
        elif case == "previous_turn":
            env.store.finish_turn(env.sid, env.turn, ExecutionOutcome.failure(ToolError("stop")))
            env.turn = env.store.start_turn(env.sid, "new turn")
        else:
            request = env.store.start_request(env.sid, env.turn, "offline")
            env.store.finish_request(env.sid, env.turn, request, (1, 1))
            read_id = "other"
            name = "file_info" if case == "other_tool" else "read_file"
            path = "missing.txt" if case == "failed_read" else "target.txt"
            calls = tool_response([call(read_id, name, path=path)])["choices"][0]["message"][
                "tool_calls"
            ]
            if case == "unfinished_read":
                env.store.append_tool_event(
                    env.sid,
                    "tool_started",
                    {
                        "turn_id": env.turn,
                        "request_id": request,
                        "tool_call_id": read_id,
                        "name": name,
                        "arguments": json.dumps({"path": path}),
                    },
                )
            else:
                ToolExecutor(env.store, create_tool_registry(tmp_path), lambda _: None).execute(
                    env.sid, env.turn, request, calls, ToolBudget(3)
                )
        _, args = pending_submit(env, read_call_id=read_id)
        with pytest.raises(ToolError):
            env.service.submit(**args)
        assert env.store.load_proposals(env.sid) == []
        assert env.target.read_bytes() == b"value = 1\n"


@pytest.mark.parametrize(
    "case", ["stale", "unseen", "duplicate", "line_truncated", "deleted", "permission", "cancel"]
)
def test_invalid_current_file_or_unread_text_never_saves_candidate(tmp_path, case):
    raw = {
        "duplicate": b"value = 1\nvalue = 1\n",
        "line_truncated": b"value = 1" + b"x" * 1600 + b"\n",
        "unseen": b"header\nvalue = 1\n",
    }.get(case, b"value = 1\n")
    read_args = {"max_lines": 1} if case == "unseen" else None
    with candidate_at(tmp_path, raw=raw, read_args=read_args) as env:
        if case == "stale":
            env.target.write_bytes(b"value = 3\n")
        elif case == "deleted":
            env.target.unlink()
        elif case == "permission":
            env.service.policy = PathPolicy(
                tmp_path, PermissionConfig(), protected_dirs=(env.target,)
            )
        elif case == "cancel":
            env.service.cancel.cancel()
        _, args = pending_submit(env)
        before = env.target.read_bytes() if env.target.exists() else None
        with pytest.raises(RequestInterrupted if case == "cancel" else ToolError):
            env.service.submit(**args)
        assert env.store.load_proposals(env.sid) == []
        assert (env.target.read_bytes() if env.target.exists() else None) == before


def test_duplicate_submission_and_turn_quota_do_not_overwrite_candidates(tmp_path):
    with candidate_at(tmp_path) as env:
        for index in range(8):
            source, args = pending_submit(env, identifier=f"submit{index}")
            proposal = env.service.submit(**args)
            with pytest.raises(ToolError):
                env.service.submit(**args)
            finish_submit(env, source, {"ok": True, "proposal_id": proposal.proposal_id})
        _, args = pending_submit(env, identifier="ninth")
        with pytest.raises(ToolError):
            env.service.submit(**args)
        assert len(env.store.load_proposals(env.sid)) == 8
        assert env.target.read_bytes() == b"value = 1\n"


def test_save_failure_rolls_back_candidate_and_propagates_storage_error(tmp_path, monkeypatch):
    with candidate_at(tmp_path) as env:
        _, args = pending_submit(env)
        log = next((tmp_path / "sessions").glob("*/*/*/*.jsonl"))
        before = log.read_bytes()
        original = os.fsync
        failed = False

        def fail_once(descriptor):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("injected proposal fsync failure")
            original(descriptor)

        monkeypatch.setattr(os, "fsync", fail_once)
        with pytest.raises(StorageError):
            env.service.submit(**args)
        assert failed and log.read_bytes() == before
        assert env.store.load_proposals(env.sid) == []
        assert env.target.read_bytes() == b"value = 1\n"


@pytest.mark.parametrize(
    "changed",
    ["read_path", "read_version", "read_request", "child", "turn", "new_text", "old_text"],
)
def test_replay_rejects_proposal_provenance_or_argument_tampering(tmp_path, changed):
    with candidate_at(tmp_path) as env:
        _, args = pending_submit(env)
        proposal = env.service.submit(**args)
        sid = env.sid
        events = env.store.load_events(sid)
        payload = next(
            event["payload"] for event in events if event["type"] == "proposal_submitted"
        )
        if changed == "read_path":
            payload["read"]["path"] = str(tmp_path / "not-read.txt")
        elif changed == "read_version":
            payload["read"]["version"] = "f" * 64
        elif changed == "read_request":
            payload["read"]["request_id"] = "d" * 32
        elif changed == "child":
            payload["owner"]["child_session_id"] = "dddddddd"
        elif changed == "turn":
            payload["turn_id"] = "e" * 32
        elif changed == "new_text":
            payload["edit"]["new_text"] = "forged change"
        else:
            payload["edit"]["old_text"] = "unseen text"
        log = next((tmp_path / "sessions").glob("*/*/*/*.jsonl"))
    log.write_bytes("".join(json.dumps(event) + "\n" for event in events).encode())
    with SessionStore(tmp_path / "sessions") as store:
        with pytest.raises(StorageError):
            store.load_proposals(sid)
    assert proposal.read.path == str(tmp_path / "target.txt")
    assert (tmp_path / "target.txt").read_bytes() == b"value = 1\n"


def test_fake_http_calls_real_submission_api_with_authoritative_read_record(tmp_path):
    """测试自备薄适配器，只验证 API 闭环；生产工具注册在 T11.2 交付。"""
    (tmp_path / "target.txt").write_bytes(b"value = 1\n")
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            data = tool_response([call("read", "read_file", path="target.txt")])
        elif len(requests) == 2:
            assert outputs(body)[-1]["path"] == str(tmp_path / "target.txt")
            data = tool_response(
                [
                    call(
                        "submit",
                        "submit_proposal",
                        read_call_id="read",
                        old_text="value = 1",
                        new_text="value = 2",
                        reason="correct value",
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["proposal_id"]
            data = answer("candidate saved; no file was changed")
        return httpx.Response(200, json=data)

    config = LLMConfig("https://unused.test", "test", "unused", stream=False)
    with (
        SessionStore(tmp_path / "sessions") as store,
        httpx.Client(transport=httpx.MockTransport(model)) as http,
    ):
        sid = store.create_session("child", "")
        service = ProposalSubmission(
            store,
            ProposalOwner("aaaaaaaa", "b" * 32, sid, "c" * 16),
            PathPolicy(tmp_path, PermissionConfig()),
            ToolLimits(),
        )
        registry = create_tool_registry(tmp_path).extended(
            {
                "submit_proposal": schema(
                    "test adapter",
                    {
                        key: ("string", key)
                        for key in ("read_call_id", "old_text", "new_text", "reason")
                    },
                    ["read_call_id", "old_text", "new_text", "reason"],
                )
            },
            {
                "submit_proposal": lambda args, _: {
                    "proposal_id": service.submit(**args).proposal_id
                }
            },
            capabilities={"submit_proposal": ToolCapability.SESSION_STATE},
        )
        conversation = Conversation(
            store,
            LLMClient(config, http),
            "",
            ToolExecutor(store, registry, lambda _: None, before_call=service.bind_source),
            AgentConfig(),
            model=config.model,
        )
        conversation.resume(sid)
        assert "no file" in conversation.send("propose the correction")
        proposal = store.load_proposals(sid)[0]
        assert proposal.edit.read_call_id == "read"
        assert store.usage(sid) == (36, False)
        assert len(store.load_messages(sid)) == 6
        assert store.load_edits(sid) == []
    assert (tmp_path / "target.txt").read_bytes() == b"value = 1\n"


def test_result_preview_without_lines_cannot_be_claimed_as_read_proof(tmp_path):
    # 反斜线在 JSON 中会转义，实际编码后超限，触发通用结果预览。
    raw = b"value = 1\n" + (b"\\" * 80 + b"\n") * 4
    with candidate_at(tmp_path, raw=raw, tool_limits=ToolLimits(max_result_chars=512)) as env:
        output = json.loads(env.store.tool_record(env.sid, env.turn, "read")["output"])
        assert "preview" in output and "lines" not in output
        _, args = pending_submit(env)
        with pytest.raises(ToolError):
            env.service.submit(**args)
        assert env.store.load_proposals(env.sid) == []
        assert env.target.read_bytes() == raw
