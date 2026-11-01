"""T8.3：同一长日志任务的输入对照、真实发布故障、备份恢复和读取预算。"""

import json
import os
import re
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from adam.config.settings import ArtifactConfig, ContextConfig, ToolLimits
from adam.contracts.errors import BudgetError, FailureCode
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_artifacts import application, read_call, reference_from
from tests.features.test_tool_loop import answer, tool_response


def command():
    """实际读取同一日志并留下执行次数；显式输出字节，不依赖平台换行转换。"""
    return [
        sys.executable,
        "-c",
        "import sys; from pathlib import Path; "
        "Path('executions.txt').open('a').write('x'); "
        "sys.stdout.buffer.write(Path('fixture.log').read_bytes())",
    ]


def prepare_log(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "fixture.log").write_bytes(b"noise data segment\n" * 2500 + b"ANSWER=42")


def seed(root):
    """先通过真实命令建立可在后续会话读取的已提交附件。"""
    prepare_log(root)
    reference = {}

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("command", "run_command", argv=command())])
        reference.update(outputs(body)[-1]["artifact"])
        return answer("saved")

    with application(root, respond) as (app, _, _):
        app.send("retain log")
        sid = app.session.session_id
    return sid, reference


def test_same_task_full_output_and_artifact_have_measured_input_difference(tmp_path):
    results = {}
    for mode in ("full", "artifact"):
        root = tmp_path / mode
        prepare_log(root)
        config = replace(
            config_at(root),
            artifacts=ArtifactConfig(enabled=mode == "artifact"),
            tool_limits=ToolLimits(max_result_chars=100000 if mode == "full" else 1024),
            context=ContextConfig(auto_compact=False, max_input_tokens=100000),
        )

        def respond(body, phase, mode=mode):
            if phase == 1:
                return tool_response([call("command", "run_command", argv=command())])
            value = outputs(body)[-1]
            if phase == 2 and mode == "artifact":
                reference = value["artifact"]
                assert reference["complete"]
                return tool_response(
                    [read_call(reference, offset=reference["saved_chars"] - 240, count=240)]
                )
            visible = value["stdout"] if mode == "full" else value["text"]
            match = re.search(r"ANSWER=(\d+)", visible)
            assert match is not None
            return answer("answer=" + match.group(1))

        with application(root, respond, config=config) as (app, store, requests):
            reply = app.send("Find the ANSWER value in the command log")
            contexts = [
                item["payload"]["context"]
                for item in store.load_events(app.session.session_id)
                if item["type"] == "request_started"
            ]
            results[mode] = {
                "answer": reply,
                "model_requests": len(requests),
                "input_bytes": sum(
                    len(
                        json.dumps(
                            {"messages": body["messages"], "tools": body["tools"]},
                            ensure_ascii=False,
                        ).encode("utf-8")
                    )
                    for body in requests
                ),
                "estimated_input_tokens": sum(item["estimated_input_tokens"] for item in contexts),
                "command_executions": len(store.load_executions(app.session.session_id)),
            }
        assert (root / "executions.txt").read_text() == "x"

    assert results["full"]["answer"] == results["artifact"]["answer"] == "answer=42"
    assert results["full"]["model_requests"] == 2 and results["artifact"]["model_requests"] == 3
    assert results["artifact"]["input_bytes"] < results["full"]["input_bytes"]
    assert results["artifact"]["estimated_input_tokens"] < results["full"]["estimated_input_tokens"]
    assert all(item["command_executions"] == 1 for item in results.values())
    # 记录实际输入与本地估算；固定假模型的 usage 不冒充真实模型计费。
    print(json.dumps(results, ensure_ascii=False, sort_keys=True))


def test_read_artifact_consumes_protected_current_turn_context_budget(tmp_path):
    sid, reference = seed(tmp_path)
    config = replace(config_at(tmp_path), tool_limits=ToolLimits(max_result_chars=8000))
    query = f"Read artifact {reference['artifact_id']} at version {reference['version']}"

    def respond(body, phase):
        assert phase == 1, "an over-budget continuation must not reach the model"
        return tool_response([read_call(reference, count=4000)])

    # 用无旧历史的实际请求计入运行状态；避免可省略的旧历史掩盖当前工具结果超限。
    with application(tmp_path, lambda *_: answer("baseline"), config=config) as (app, store, _):
        app.send(query)
        baseline = next(
            e["payload"]["context"]["estimated_input_tokens"]
            for e in store.load_events(app.session.session_id)
            if e["type"] == "request_started"
        )
    limited = replace(
        config,
        context=ContextConfig(
            auto_compact=False, max_input_tokens=baseline + 600, safety_margin_tokens=0
        ),
    )
    with application(tmp_path, respond, config=limited) as (app, store, requests):
        app.resume(sid)
        before = [item.raw for item in app.history()]
        usage_before = store.usage(sid)
        with pytest.raises(BudgetError) as error:
            app.send(query)
        assert error.value.code == FailureCode.CONTEXT_INPUT_LIMIT
        assert len(requests) == 1
        assert [item.raw for item in app.history()] == before
        assert store.usage(sid) == (usage_before[0] + 12, False)
        events = store.load_events(sid)
        last_result = next(
            item["payload"]["output"]
            for item in reversed(events)
            if item["type"] == "tool_finished"
        )
        assert len(json.loads(last_result)["text"]) == 4000
        assert events[-1]["payload"]["reason"] == "context_input_limit"
        assert reference_from(store, sid).artifact_id == reference["artifact_id"]
        assert len(store.load_executions(sid)) == 1
    assert (tmp_path / "executions.txt").read_text() == "x"


def test_real_attachment_publish_failure_keeps_old_data_and_command_fact(tmp_path, monkeypatch):
    sid, reference = seed(tmp_path)
    artifact = next((tmp_path / "sessions" / "_artifacts").glob("*/*.json"))
    original = artifact.read_bytes()
    replace_file = os.replace

    def fail_publish(source, destination):
        # 注入真实原子发布调用的 OS 失败；会话与记忆的其他发布正常执行。
        if Path(source).name.startswith(".artifact-"):
            raise PermissionError("test publication failure")
        return replace_file(source, destination)

    monkeypatch.setattr(os, "replace", fail_publish)

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("command", "run_command", argv=command())])
        value = outputs(body)[-1]
        assert value["artifact_unavailable"] and "artifact" not in value
        assert value["exit_code"] == 0 and value["status"] == "completed"
        return answer("preview only")

    with application(tmp_path, respond) as (app, store, requests):
        app.resume(sid)
        assert app.send("run once more") == "preview only"
        assert len(requests) == 2
        assert len(store.load_executions(sid)) == 2
        assert reference_from(store, sid).artifact_id == reference["artifact_id"]
    assert artifact.read_bytes() == original
    assert (tmp_path / "executions.txt").read_text() == "xx"
    assert list(artifact.parent.glob(".artifact-*.tmp")) == []


def test_whole_session_backup_restores_attachment_without_changing_source(tmp_path):
    original = tmp_path / "original"
    sid, reference = seed(original)
    source_files = {
        path.relative_to(original / "sessions"): path.read_bytes()
        for path in (original / "sessions").rglob("*")
        if path.is_file() and path.suffix in {".json", ".jsonl"}
    }
    restored = tmp_path / "restored"
    shutil.copytree(original / "sessions", restored / "sessions")

    def respond(body, phase):
        if phase == 1:
            return tool_response(
                [read_call(reference, offset=reference["saved_chars"] - 240, count=240)]
            )
        value = outputs(body)[-1]
        assert value["complete"] and "ANSWER=42" in value["text"]
        return answer("restored evidence")

    with application(restored, respond) as (app, store, requests):
        app.resume(sid)
        assert app.send("read saved log after migration") == "restored evidence"
        assert len(requests) == 2 and len(store.load_executions(sid)) == 1
    assert all(
        (original / "sessions" / path).read_bytes() == raw for path, raw in source_files.items()
    )
    assert not (restored / "executions.txt").exists()
