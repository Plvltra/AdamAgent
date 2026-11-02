"""T8.2：假模型驱动真实命令、文本和附件；检查回传、来源、隔离与失败边界。"""

import json
import sys
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from adam.bootstrap import build_application
from adam.config.settings import ArtifactConfig, ToolLimits
from adam.contracts.errors import StorageError
from adam.llm.client import LLMClient
from adam.session.artifacts import ArtifactStore
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


@contextmanager
def application(root, respond, *, config=None, approvals=None):
    """仅远端响应为模拟；通过标准组装和真实文件/进程检查完整工具边界。"""
    config = config or replace(config_at(root), tool_limits=ToolLimits(max_result_chars=1024))
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=respond(body, len(requests)))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            root,
            confirm_command=lambda request: (
                approvals.append(request) is None if approvals is not None else True
            ),
        )
        try:
            yield app, store, requests
        finally:
            app.close()


def populate(root):
    """生成转义后超长的真实读取结果，不伪造工具成功或附件来源。

    目录现在原生分页，不再借目录溢出触发附件；保持原隔离、来源和失败验收。
    """
    (root / "artifact-source.txt").write_text(('"' * 256 + "\n") * 4, encoding="utf-8")


def reference_from(store, sid):
    records = [
        item["payload"] for item in store.load_events(sid) if item["type"] == "artifact_saved"
    ]
    assert len(records) == 1
    return store.load_artifact(sid, records[0]["artifact_id"])


def read_call(reference, **changes):
    return call(
        f"page-{changes.get('offset', 0)}",
        "read_artifact",
        **{"artifact_id": reference["artifact_id"], "version": reference["version"], **changes},
    )


def test_command_tail_survives_capture_preview_and_is_read_by_model(tmp_path):
    expected = "汉字-日志\n" * 6000 + "TAIL-EVIDENCE"
    approvals = []
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(('汉字-日志\\n' * 6000 + 'TAIL-EVIDENCE').encode('utf-8')); "
        "sys.stderr.write('warning'); sys.exit(7)",
    ]

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("command", "run_command", argv=command)])
        value = outputs(body)[-1]
        if phase == 2:
            assert value["status"] == "completed" and value["exit_code"] == 7
            assert value["ok"] is True and value["truncated"]
            reference = value["artifact"]
            assert reference["complete"] and reference["saved_chars"] > 30000
            return tool_response(
                [read_call(reference, offset=reference["saved_chars"] - 180, count=180)]
            )
        assert "TAIL-EVIDENCE" in value["text"] and value["next_offset"] is None
        assert value["complete"]
        assert all(
            len(item["content"]) <= 1024 for item in body["messages"] if item["role"] == "tool"
        )
        return answer("tail observed; command failed with 7")

    with application(tmp_path, respond, approvals=approvals) as (app, store, requests):
        assert app.send("inspect long command output") == "tail observed; command failed with 7"
        sid = app.session.session_id
        reference = reference_from(store, sid)
        assert reference.source.tool_name == "run_command"
        events = store.load_events(sid)
        started = next(item["payload"] for item in events if item["type"] == "tool_started")
        assert reference.source.tool_call_id == started["tool_call_id"]
        assert reference.source.request_id == started["request_id"]
        saved = ArtifactStore(store.root / "_artifacts", max_read_chars=100000).read(
            sid, reference.artifact_id, expected_version=reference.version, count=100000
        )
        actual = json.loads(saved.text)["stdout"]
        assert len(actual) == len(expected)
        assert actual == expected
        execution = store.load_executions(sid)[0]
        assert execution["exit_code"] == 7 and execution["truncated"]
        assert len(execution["stdout"]) < len(expected)
        assert len(approvals) == 1 and len(requests) == 3


def test_generic_long_result_reopens_and_current_session_cannot_claim_other_reference(tmp_path):
    populate(tmp_path)
    returned = {}

    def first(body, phase):
        if phase == 1:
            return tool_response([call("source", "read_file", path="artifact-source.txt")])
        returned.update(outputs(body)[-1]["artifact"])
        return answer()

    with application(tmp_path, first) as (app, store, _):
        app.send("list")
        original = app.session.session_id
        reference = reference_from(store, original)
        assert reference.source.tool_name == "read_file"

    def later(body, phase):
        if phase in {1, 3}:
            return tool_response([read_call(returned, count=100)])
        return answer()

    with application(tmp_path, later) as (app, store, requests):
        app.resume(original)
        app.select_mode("plan")
        app.send("read saved output")
        assert outputs(requests[1])[-1]["ok"] is True
        app.new("isolated")
        app.send("try previous reference")
        rejected = outputs(requests[3])[-1]
        assert rejected["ok"] is False and "执行来源" in rejected["error"]
        assert store.load_artifact(app.session.session_id, returned["artifact_id"]) is None


@pytest.mark.parametrize("failure", ["write", "metadata"])
def test_attachment_failure_never_reexecutes_command(tmp_path, monkeypatch, failure):
    approvals = []
    marker = tmp_path / "executed.txt"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; "
        "p=Path('executed.txt'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "sys.stdout.write('long-output' * 3000)",
    ]

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("command", "run_command", argv=command)])
        value = outputs(body)[-1]
        assert value["artifact_unavailable"] and "artifact" not in value
        assert value["status"] == "completed" and value["exit_code"] == 0
        return answer("output unavailable")

    def fail(*args, **kwargs):
        raise StorageError("injected at artifact " + failure)

    # 分别模拟正文存储边界和正文已发布后的来源日志边界；命令本身真实运行。
    monkeypatch.setattr(
        ArtifactStore if failure == "write" else SessionStore,
        "save" if failure == "write" else "append_artifact",
        fail,
    )
    with application(tmp_path, respond, approvals=approvals) as (app, store, requests):
        if failure == "metadata":
            with pytest.raises(StorageError):
                app.send("execute once")
            assert len(requests) == 1
            assert store.load_messages(app.session.session_id) == []
        else:
            assert app.send("execute once") == "output unavailable"
            assert len(requests) == 2
        assert len(approvals) == 1 and marker.read_text() == "x"
        assert store.load_executions(app.session.session_id)[0]["exit_code"] == 0
        assert not any(
            item["type"] == "artifact_saved" for item in store.load_events(app.session.session_id)
        )


@pytest.mark.parametrize("change", ["missing", "metadata", "version"])
def test_reader_rejects_lost_changed_and_stale_artifact(tmp_path, change):
    populate(tmp_path)

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("source", "read_file", path="artifact-source.txt")])
        if phase == 2:
            reference = outputs(body)[-1]["artifact"]
            path = next((tmp_path / "sessions" / "_artifacts").glob("*/*.json"))
            if change == "missing":
                path.unlink()
            elif change == "metadata":
                bundle = json.loads(path.read_text(encoding="utf-8"))
                bundle["reference"]["source"]["tool_name"] = "forged"
                path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            else:
                reference["version"] = "0" * 64
            return tool_response([read_call(reference)])
        assert outputs(body)[-1]["ok"] is False
        return answer("unavailable")

    with application(tmp_path, respond) as (app, _, requests):
        assert app.send("read artifact") == "unavailable"
        assert len(requests) == 3


def test_attachment_directory_is_not_an_ordinary_readable_path(tmp_path):
    populate(tmp_path)

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("source", "read_file", path="artifact-source.txt")])
        if phase == 2:
            path = next((tmp_path / "sessions" / "_artifacts").glob("*/*.json"))
            return tool_response([call("bypass", "read_file", path=str(path))])
        assert "保留目录" in outputs(body)[-1]["error"]
        return answer()

    with application(tmp_path, respond) as (app, _, _):
        app.send("try ordinary path")


@pytest.mark.parametrize("enabled", [True, False])
def test_disabled_attachments_keep_existing_short_result_and_no_directory(tmp_path, enabled):
    config = replace(config_at(tmp_path), artifacts=ArtifactConfig(enabled=enabled))
    (tmp_path / "short.txt").write_text("short", encoding="utf-8")

    def respond(body, phase):
        assert ("read_artifact" in {item["function"]["name"] for item in body["tools"]}) is enabled
        if phase == 1:
            return tool_response([call("read", "read_file", path="short.txt")])
        assert outputs(body)[-1]["lines"][0]["text"] == "short"
        assert "artifact" not in outputs(body)[-1]
        return answer()

    with application(tmp_path, respond, config=config) as (app, store, _):
        app.send("short read")
        assert not (store.root / "_artifacts").exists()


def test_escaped_page_respects_result_limit_and_next_offset(tmp_path):
    # 目录名中不能含控制字符；用真实命令生成转义密集的附件文本。
    config = replace(config_at(tmp_path), tool_limits=ToolLimits(max_result_chars=512))
    pages = []
    cursor = 0
    reference = {}

    def respond(body, phase):
        nonlocal cursor
        if phase == 1:
            return tool_response(
                [
                    call(
                        "command",
                        "run_command",
                        argv=[sys.executable, "-c", "import sys; sys.stdout.write(chr(0) * 300)"],
                    )
                ]
            )
        value = outputs(body)[-1]
        if phase == 2:
            reference.update(value["artifact"])
        else:
            assert value["offset"] == cursor
            pages.append(value["text"])
            assert len(body["messages"][-1]["content"]) <= 512
            if len(pages) == 2:
                return answer()
            cursor = value["next_offset"]
            assert cursor == sum(map(len, pages))
        return tool_response([read_call(reference, offset=cursor)])

    with application(tmp_path, respond, config=config) as (app, store, _):
        app.send("read escaped pages")
        saved = reference_from(store, app.session.session_id)
        raw = (
            ArtifactStore(store.root / "_artifacts")
            .read(app.session.session_id, saved.artifact_id, expected_version=saved.version)
            .text
        )
        assert "".join(pages) == raw[: sum(map(len, pages))]
        assert len(list((store.root / "_artifacts").glob("*/*.json"))) == 1


def test_capture_and_storage_limit_never_claim_complete_output(tmp_path):
    config = replace(
        config_at(tmp_path),
        tool_limits=ToolLimits(max_result_chars=512),
        artifacts=ArtifactConfig(max_bytes=1500),
    )

    def respond(body, phase):
        if phase == 1:
            return tool_response(
                [
                    call(
                        "command",
                        "run_command",
                        argv=[sys.executable, "-c", "import sys; sys.stdout.write('汉' * 3000)"],
                    )
                ]
            )
        value = outputs(body)[-1]
        assert value["artifact"]["complete"] is False
        return answer()

    with application(tmp_path, respond, config=config) as (app, store, _):
        app.send("partial output")
        reference = reference_from(store, app.session.session_id)
        assert not reference.complete and reference.total_bytes is None
        assert reference.saved_bytes <= 1500
