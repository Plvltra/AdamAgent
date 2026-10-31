"""T11.3 独立进程：真实主子 HTTP 与编辑，故障暂停仅存在于验收入口。"""

import json
import sys
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import httpx

from adam.bootstrap import build_application
from adam.contracts.errors import AgentError
from adam.llm.client import LLMClient
from adam.session.diagnostics import read_report
from adam.session.storage import SessionStore
from adam.tool_calls.edit_tools import EditTools
from tests.features.support.undo_worker import PartialWriter, blocked_actions, hold
from tests.features.test_proposal_application import config_at


def install_fault(root, target, phase, ready):
    """在实际已落盘边界暂停；子任务边界由父测试额外等待主工具进入 wait_agents。"""
    append = SessionStore._append

    def append_and_hold(self, session_id, data):
        append(self, session_id, data)
        main_markers = {
            "edit_planned": b'"type": "edit_planned"',
            "edit_started": b'"type": "edit_started"',
            "edit_finished": b'"type": "edit_finished"',
            "turn_completed": b'"type": "turn_completed"',
        }
        if self.root == root and phase in main_markers and main_markers[phase] in data:
            hold(ready)
        if root in self.root.parents:
            marker = {
                "child_saved": b'"type": "proposal_submitted"',
                "child_committed": b'"type": "turn_completed"',
            }.get(phase)
            if marker and marker in data:
                hold(ready)

    SessionStore._append = append_and_hold
    if phase == "child_partial":
        original_open = Path.open

        def partial_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if root / "_subagents" in path.parents and path.suffix == ".jsonl" and mode == "r+b":
                return PartialWriter(stream, ready, b'"type": "proposal_submitted"')
            return stream

        Path.open = partial_open
    if phase == "after_replace":
        apply = EditTools.apply

        def apply_and_hold(self, plan):
            apply(self, plan)
            if plan.path == target:
                hold(ready)

        EditTools.apply = apply_and_hold


def child_state(root, session_id):
    """只在本进程完成恢复或工作后读取；不向外部观察者暴露正在替换的索引文件。"""
    group = root / "_subagents" / session_id
    index = json.loads((group / "index.json").read_text(encoding="utf-8"))
    result = []
    with SessionStore(group / "sessions") as store:
        for agent in index["agents"]:
            child_id = agent["agent_id"]
            path = next(store.root.glob("*/*/*/" + child_id + ".jsonl"))
            result.append(
                {
                    "agent": agent,
                    "proposals": [item.to_dict() for item in store.load_proposals(child_id)],
                    "report": read_report(path),
                    "history": [message.raw for message in store.load_messages(child_id)],
                    "edits": store.load_edits(child_id),
                }
            )
    return result


def main():
    """每次使用新的标准应用；恢复和查询不发请求，只有显式 action 才再次执行。"""
    action, workspace_text, session_id, url, phase, ready_text = sys.argv[1:]
    workspace, ready = Path(workspace_text), Path(ready_text)
    config = config_at(workspace)
    config = replace(config, llm=replace(config.llm, base_url=url))
    approvals = []
    result = {"error": None}

    @contextmanager
    def child_client(cancel):
        with httpx.Client(trust_env=False, timeout=5) as http:
            yield LLMClient(config.llm, http)

    def started_tool(name):
        if name == "wait_agents":
            (workspace / "parent-waiting").write_text("ready", encoding="utf-8")

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(trust_env=False, timeout=5) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_edit=lambda plan: approvals.append(plan.diff) or True,
            child_client_factory=child_client,
            on_tool=started_tool,
        )
        try:
            if session_id:
                app.resume(session_id)
                app.usage()  # 通过原账本选择路径恢复子会话与委派索引。
            else:
                app.new("proposal crash")
                session_id = app.session.session_id
                (workspace / "session-id").write_text(session_id, encoding="utf-8")
            install_fault(store.root, workspace / "target.py", phase, ready)
            try:
                if action == "run":
                    app.send("create proposal")
                elif action == "apply":
                    children = child_state(store.root, session_id)
                    proposal = next(item for child in children for item in child["proposals"])
                    args = {
                        "agent_id": proposal["owner"]["child_session_id"],
                        "run_id": proposal["owner"]["run_id"],
                        "proposal_id": proposal["proposal_id"],
                        "path": proposal["read"]["path"],
                    }
                    app.send("apply saved " + json.dumps(args))
                elif action == "undo":
                    app.undo()
                elif action == "blocked":
                    result["blocked"] = blocked_actions(app)
                elif action != "inspect":
                    raise AssertionError("unknown action")
            except AgentError as error:
                result["error"] = {"code": error.code, "message": str(error)}
            usage = app.usage()
            path = next(store.root.glob("*/*/*/" + session_id + ".jsonl"))
            result.update(
                session_id=session_id,
                report=read_report(path),
                history=[message.raw for message in app.history()],
                usage=asdict(usage),
                continuation=asdict(app.continuation_status()),
                children=child_state(store.root, session_id),
                approvals=approvals,
                edits=store.load_edits(session_id),
            )
        finally:
            app.close()
    result["live_children"] = [
        thread.name for thread in threading.enumerate() if thread.name.startswith("adam-child")
    ]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
