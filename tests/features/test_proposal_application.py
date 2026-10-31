"""T11.2：实际子线程提出候选，主任务经原批准、编辑日志和检查点应用。"""

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from adam.config.settings import (
    AgentConfig,
    AppConfig,
    LLMConfig,
    PermissionConfig,
    ReflectionConfig,
    ToolLimits,
)
from adam.contracts.errors import ApprovalDeniedError, BudgetError, EditLogError, StorageError
from adam.session.storage import SessionStore
from tests.features.test_hook_acceptance import example_hooks
from tests.features.test_independent_review import reviewer
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_hooks import hooks_at
from tests.features.test_tool_loop import answer, tool_response


def config_at(root, **changes):
    return replace(
        AppConfig(
            LLMConfig("https://unused.test", "test", "unused", stream=False),
            root / "sessions",
            "",
            agent=AgentConfig(max_session_tokens=500000),
            reflection=ReflectionConfig(enabled=False),
        ),
        **changes,
    )


@contextmanager
def proposal_workflow(
    root, *, config=None, directory="", child_failure=False, replacement="value = 2"
):
    """远端响应用固定脚本，本地读取、派发、候选、批准和编辑均经过标准组装。"""
    folder = root / directory
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "target.py"
    target.write_bytes(b"value = 1\n")
    env = SimpleNamespace(
        target=target,
        parent_requests=[],
        child_requests=[],
        approvals=[],
        notices=[],
        changes={},
        before_apply=None,
        on_confirm=None,
        approve=True,
        result=None,
        proposal_id=None,
        followup=None,
    )

    def confirm(plan):
        env.approvals.append((threading.get_ident(), plan))
        if env.on_confirm:
            env.on_confirm()
        return env.approve

    def child(request):
        body = json.loads(request.content)
        env.child_requests.append(body)
        names = {item["function"]["name"] for item in body["tools"]}
        if "submit_proposal" not in names:
            return reviewer(request)
        assert not {"apply_proposal", "collect_proposals"} & names
        phase = len(env.child_requests)
        if phase == 1:
            response = tool_response([call("read", "read_file", path=str(target))])
        elif phase == 2:
            assert outputs(body)[-1]["ok"]
            response = tool_response(
                [
                    call(
                        "submit",
                        "submit_proposal",
                        read_call_id="read",
                        old_text="value = 1",
                        new_text=replacement,
                        reason="fix the value",
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["status"] == "submitted"
            env.proposal_id = outputs(body)[-1]["proposal_id"]
            if child_failure == "unknown":
                return httpx.Response(503, text="injected failure after candidate saved")
            response = answer("candidate saved, no file changed")
            if child_failure:
                # 已知用量但消息协议错误，让主任务仍能检查失败子轮的候选准入。
                response["choices"][0]["message"]["role"] = "user"
        return httpx.Response(200, json=response)

    def parent(request):
        body = json.loads(request.content)
        env.parent_requests.append(body)
        names = {item["function"]["name"] for item in body["tools"]}
        assert {"apply_proposal", "collect_proposals"} <= names
        assert "submit_proposal" not in names
        phase = len(env.parent_requests)
        if phase == 1:
            response = tool_response(
                [call("spawn", "spawn_agent", name="helper", task="propose correction")]
            )
        elif phase == 2:
            spawned = outputs(body)[-1]
            env.agent_id, env.run_id = spawned["agent_id"], spawned["run_id"]
            response = tool_response([call("wait", "wait_agents", agent_ids=[env.agent_id])])
        elif phase == 3:
            assert target.read_bytes() == b"value = 1\n"
            if child_failure:
                response = tool_response(
                    [
                        call(
                            "apply",
                            "apply_proposal",
                            agent_id=env.agent_id,
                            run_id=env.run_id,
                            proposal_id=env.proposal_id,
                            path=str(target),
                        )
                    ]
                )
            else:
                response = tool_response(
                    [call("collect", "collect_proposals", agent_id=env.agent_id, run_id=env.run_id)]
                )
        elif env.followup:
            response = env.followup(body)
        elif phase == 4 and not child_failure:
            collected = outputs(body)[-1]
            env.collected = collected
            env.args = {
                key: collected[key] for key in ("agent_id", "run_id", "proposal_id", "path")
            }
            if env.before_apply:
                env.before_apply()
            response = tool_response([call("apply", "apply_proposal", **(env.args | env.changes))])
        else:
            env.result = outputs(body)[-1]
            response = answer("task handled")
        return httpx.Response(200, json=response)

    with application(
        root,
        parent,
        child,
        config=config or config_at(root),
        notices=env.notices,
        confirm_edit=confirm,
    ) as (app, store):
        env.app, env.store = app, store
        yield env


def test_real_child_proposal_uses_parent_approval_checkpoint_report_and_undo(tmp_path):
    with proposal_workflow(tmp_path) as env:
        assert env.app.send("delegate the correction") == "task handled"
        sid = env.app.session.session_id
        assert env.target.read_bytes() == b"value = 2\n"
        assert env.result["status"] == "applied"
        assert env.approvals[0][0] == threading.get_ident()
        assert len(env.approvals) == 1 and "+value = 2" in env.approvals[0][1].diff
        edit = env.store.load_edits(sid)[0]
        assert edit["proposal"]["proposal_id"] == env.proposal_id
        assert edit["proposal"]["owner"]["child_session_id"] == env.agent_id
        checkpoint = env.store.load_edit_checkpoint(sid, edit["edit_id"], max_bytes=1024)
        assert checkpoint.original == b"value = 1\n"
        report = env.app.execution_report()
        assert len(report.actions) == 1
        assert report.actions[0].kind == "apply_proposal"
        assert report.actions[0].status == "applied"
        assert env.app.usage().tokens == 8 * 12
        env.app.undo()
        assert env.target.read_bytes() == b"value = 1\n"
        assert len(env.approvals) == 2
        index = json.loads(index_for(env.app).read_text(encoding="utf-8"))
        child_id = index["agents"][0]["agent_id"]
        with SessionStore(env.store.root / "_subagents" / sid / "sessions") as children:
            assert children.load_edits(child_id) == []
            assert children.load_proposals(child_id)[0].proposal_id == env.proposal_id

        def reapply(body):
            if len(env.parent_requests) == 6:
                return tool_response([call("again", "apply_proposal", **env.args)])
            env.result = outputs(body)[-1]
            return answer("not repeated")

        env.followup = reapply
        assert env.app.send("try the same saved proposal") == "not repeated"
        assert env.result["ok"] is False
        assert len(env.store.load_edits(sid)) == 1 and len(env.approvals) == 2
        assert env.target.read_bytes() == b"value = 1\n"


@pytest.mark.parametrize(
    "case", ["denied", "plan", "budget", "stale", "wrong_path", "wrong_run", "wrong_id"]
)
def test_rejected_application_has_no_file_effect(tmp_path, case):
    config = config_at(tmp_path)
    if case == "budget":
        config = replace(config, agent=replace(config.agent, max_edit_attempts=0))
    with proposal_workflow(tmp_path, config=config) as env:
        if case == "denied":
            env.approve = False
        elif case == "plan":
            env.app.select_mode("plan")
        elif case == "stale":
            env.before_apply = lambda: env.target.write_bytes(b"external = 9\n")
        elif case == "wrong_path":
            env.changes["path"] = str(tmp_path / "other.py")
        elif case == "wrong_run":
            env.changes["run_id"] = "f" * 16
        elif case == "wrong_id":
            env.changes["proposal_id"] = "f" * 16
        if case in {"denied", "budget"}:
            with pytest.raises(ApprovalDeniedError if case == "denied" else BudgetError):
                env.app.send("propose then apply")
        else:
            env.app.send("propose then apply")
            assert env.result["ok"] is False
        expected = b"external = 9\n" if case == "stale" else b"value = 1\n"
        assert env.target.read_bytes() == expected
        edits = env.store.load_edits(env.app.session.session_id)
        if case == "denied":
            assert len(edits) == len(env.approvals) == 1 and edits[0]["status"] == "denied"
        else:
            assert edits == [] and env.approvals == []


def test_failed_child_candidate_cannot_be_applied(tmp_path):
    with proposal_workflow(tmp_path, child_failure=True) as env:
        env.app.send("use child suggestion")
        assert env.proposal_id
        assert env.result["ok"] is False
        assert env.target.read_bytes() == b"value = 1\n"
        assert not env.approvals
        assert env.store.load_edits(env.app.session.session_id) == []


def test_approval_race_preserves_external_change_and_records_conflict(tmp_path):
    with proposal_workflow(tmp_path) as env:
        env.on_confirm = lambda: env.target.write_bytes(b"external = 9\n")
        env.app.send("approve after checking diff")
        assert env.result["status"] == "conflict"
        assert env.target.read_bytes() == b"external = 9\n"
        edit = env.store.load_edits(env.app.session.session_id)[0]
        assert edit["status"] == "conflict" and edit["proposal"]["proposal_id"] == env.proposal_id
        assert len(env.approvals) == 1


@pytest.mark.parametrize(
    "directory,replacement,status",
    [
        ("vendor", "value = 2", "hook_blocked"),
        ("src", "value =", "applied"),
    ],
)
def test_real_hook_examples_cover_proposal_path_and_actual_result(
    tmp_path, directory, replacement, status
):
    config = config_at(tmp_path, hooks=example_hooks())
    with proposal_workflow(
        tmp_path, config=config, directory=directory, replacement=replacement
    ) as env:
        env.app.send("propose and apply with local checks")
        assert env.result["status"] == status
        if status == "hook_blocked":
            assert env.target.read_bytes() == b"value = 1\n" and not env.approvals
        else:
            assert env.target.read_bytes() == b"value =\n" and len(env.approvals) == 1
            assert any("后置 Hook 检查失败" in note for note in env.notices)


def test_checkpoint_failure_before_write_preserves_target(tmp_path, monkeypatch):
    with proposal_workflow(tmp_path) as env:
        original_replace = os.replace

        def fail(source, target):
            if "_checkpoints" in Path(target).parts:
                raise OSError("injected checkpoint publish failure")
            original_replace(source, target)

        def install_failure():
            # 提案已真实提交及收集，只在主编辑发布检查点的实际 IO 边界注入故障。
            monkeypatch.setattr(os, "replace", fail)

        env.before_apply = install_failure
        with pytest.raises(EditLogError):
            env.app.send("apply candidate")
        assert env.target.read_bytes() == b"value = 1\n"
        edit = env.store.load_edits(env.app.session.session_id)[0]
        assert edit["status"] == "planned"
        assert edit["proposal"]["proposal_id"] == env.proposal_id


def test_unknown_child_usage_stops_before_any_proposal_application(tmp_path):
    with proposal_workflow(tmp_path, child_failure="unknown") as env:
        with pytest.raises(BudgetError, match="用量未知"):
            env.app.send("use the candidate")
        assert env.proposal_id and env.app.usage().unknown
        assert env.target.read_bytes() == b"value = 1\n"
        assert env.approvals == [] and env.store.load_edits(env.app.session.session_id) == []


@pytest.mark.parametrize("allow_write", [False, True])
def test_read_authorization_does_not_grant_parent_write_permission(tmp_path, allow_write):
    workspace, outside = tmp_path / "project", tmp_path / "outside"
    workspace.mkdir()
    config = config_at(
        workspace,
        permissions=PermissionConfig(
            additional_read_dirs=(outside,),
            additional_write_dirs=(outside,) if allow_write else (),
        ),
    )
    with proposal_workflow(workspace, config=config, directory="../outside") as env:
        env.app.send("propose an outside edit")
        assert env.result["ok"] is allow_write
        assert env.target.read_bytes() == (b"value = 2\n" if allow_write else b"value = 1\n")
        assert len(env.approvals) == int(allow_write)


def test_long_proposal_pages_preserve_identity_and_exact_payload_without_applying(tmp_path):
    replacement = "新内容\\引号" * 500
    config = config_at(
        tmp_path,
        agent=AgentConfig(max_model_requests=40, max_tool_calls=60, max_session_tokens=500000),
        tool_limits=ToolLimits(max_result_chars=1600),
    )
    pages = []
    with proposal_workflow(tmp_path, config=config, replacement=replacement) as env:

        def collect_pages(body):
            page = outputs(body)[-1]
            assert page["status"] == "collected" and page["proposal_id"] == env.proposal_id
            assert len(json.dumps(page, ensure_ascii=False)) <= 1600
            assert page["path"] == str(env.target)
            assert page["offset"] == sum(len(item["text"]) for item in pages)
            pages.append(page)
            if page["next_offset"] is not None:
                return tool_response(
                    [
                        call(
                            "page" + str(len(pages)),
                            "collect_proposals",
                            agent_id=env.agent_id,
                            run_id=env.run_id,
                            offset=page["next_offset"],
                        )
                    ]
                )
            return answer("reviewed only")

        env.followup = collect_pages
        assert env.app.send("only inspect the proposal") == "reviewed only"
        assert len(pages) > 1 and all(page["next_index"] is None for page in pages)
        payload = json.loads("".join(page["text"] for page in pages))
        assert payload["edit"]["new_text"] == replacement
        assert payload["owner"]["child_session_id"] == env.agent_id
        assert env.target.read_bytes() == b"value = 1\n" and env.approvals == []


def test_candidate_target_hash_is_checked_again_before_approval(tmp_path):
    with proposal_workflow(tmp_path) as env:

        def corrupt_candidate():
            root = env.store.root / "_subagents" / env.app.session.session_id / "sessions"
            path = next(root.glob("*/*/*/" + env.agent_id + ".jsonl"))
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            event = next(item for item in events if item["type"] == "proposal_submitted")
            event["payload"]["target_version"] = "f" * 64
            path.write_bytes("".join(json.dumps(item) + "\n" for item in events).encode())

        env.before_apply = corrupt_candidate
        env.app.send("apply the candidate")
        assert env.result["ok"] is False and "目标版本" in env.result["error"]
        assert env.target.read_bytes() == b"value = 1\n" and env.approvals == []
        assert env.store.load_edits(env.app.session.session_id) == []


@pytest.mark.parametrize("outcome", ["applied", "conflict"])
def test_plan_completion_requires_actual_proposal_edit_success(tmp_path, outcome):
    with proposal_workflow(tmp_path) as env:
        if outcome == "conflict":
            env.on_confirm = lambda: env.target.write_bytes(b"external = 9\n")

        def finish_with_plan(body):
            phase = len(env.parent_requests)
            if phase == 4:
                collected = outputs(body)[-1]
                args = {
                    key: collected[key] for key in ("agent_id", "run_id", "proposal_id", "path")
                }
                return tool_response([call("apply", "apply_proposal", **args)])
            if phase == 5:
                assert outputs(body)[-1]["status"] == outcome
                return tool_response(
                    [
                        call(
                            "plan",
                            "update_plan",
                            steps=[
                                {
                                    "step_id": "fix",
                                    "content": "apply proposal",
                                    "status": "completed",
                                    "evidence": ["apply"],
                                }
                            ],
                        )
                    ]
                )
            assert outputs(body)[-1]["ok"] is (outcome == "applied")
            return answer("checked real outcome")

        env.followup = finish_with_plan
        env.app.send("apply and track completion")
        plan = env.app.plan_report()
        assert (plan is not None) is (outcome == "applied")


def test_pending_independent_review_blocks_proposal_application(tmp_path):
    with proposal_workflow(tmp_path) as env:

        def review_then_apply(body):
            phase = len(env.parent_requests)
            if phase == 4:
                collected = outputs(body)[-1]
                args = {
                    key: collected[key] for key in ("agent_id", "run_id", "proposal_id", "path")
                }
                return tool_response(
                    [
                        call(
                            "review",
                            "request_review",
                            goal="verify original",
                            criteria="value = 1",
                            paths=[str(env.target)],
                        ),
                        call("apply", "apply_proposal", **args),
                    ]
                )
            if phase == 5:
                assert "审查尚未收集" in outputs(body)[-1]["error"]
                return tool_response(
                    [
                        call(
                            "collect_review",
                            "collect_review",
                            review_id=outputs(body)[-2]["review_id"],
                        )
                    ]
                )
            assert outputs(body)[-1]["status"] == "completed"
            return answer("reviewed without applying")

        env.followup = review_then_apply
        env.app.send("review before applying")
        assert env.target.read_bytes() == b"value = 1\n" and env.approvals == []
        assert env.store.load_edits(env.app.session.session_id) == []


def test_new_directory_rule_is_sent_before_proposal_can_edit(tmp_path):
    with proposal_workflow(tmp_path, directory="src") as env:
        env.before_apply = lambda: (tmp_path / "src" / "AGENTS.md").write_text(
            "RULE_MARKER: inspect validation before changing this directory.", encoding="utf-8"
        )
        env.app.send("apply only after reading current rules")
        assert env.result["status"] == "deferred"
        assert "RULE_MARKER" in json.dumps(env.parent_requests[-1], ensure_ascii=False)
        assert env.target.read_bytes() == b"value = 1\n" and env.approvals == []


def test_forged_audit_path_is_rejected_before_hook_or_approval(tmp_path):
    config = config_at(tmp_path, hooks=hooks_at(tmp_path))
    with proposal_workflow(tmp_path, config=config) as env:
        env.changes["path"] = str(tmp_path / "innocent.py")
        env.app.send("cannot hide the real target")
        assert env.result["ok"] is False
        assert not (tmp_path / "before_tool-apply.json").exists()
        assert env.target.read_bytes() == b"value = 1\n" and env.approvals == []


@pytest.mark.parametrize(
    "changed",
    [
        "parent",
        "child",
        "run",
        "path",
        "before_version",
        "target_version",
        "finished_candidate",
    ],
)
def test_parent_log_rejects_changed_candidate_identity_or_edit_versions(tmp_path, changed):
    with proposal_workflow(tmp_path) as env:
        env.app.send("apply the correction")
        sid = env.app.session.session_id
        events = env.store.load_events(sid)
        planned = next(event["payload"] for event in events if event["type"] == "edit_planned")
        if changed == "parent":
            planned["proposal"]["owner"]["parent_session_id"] = "ffffffff"
        elif changed == "child":
            planned["proposal"]["owner"]["child_session_id"] = "ffffffff"
        elif changed == "run":
            planned["proposal"]["owner"]["run_id"] = "f" * 16
        elif changed == "path":
            planned["path"] = str(tmp_path / "different.py")
        elif changed in {"before_version", "target_version"}:
            planned[changed] = "f" * 64
        else:
            finished = next(
                event["payload"] for event in events if event["type"] == "edit_finished"
            )
            finished["proposal"] = {**planned["proposal"], "proposal_id": "f" * 16}
        path = next(env.store.root.glob("*/*/*/" + sid + ".jsonl"))
    path.write_bytes("".join(json.dumps(event) + "\n" for event in events).encode())
    with SessionStore(tmp_path / "sessions") as store:
        with pytest.raises(StorageError):
            store.load_edits(sid)
    assert (tmp_path / "target.py").read_bytes() == b"value = 2\n"
