"""B18.2：真实命令的尾部进入模型预览；配额、原始内容和最终 JSON 大小分别验收。"""

import json
import sys
from copy import deepcopy
from dataclasses import replace

import pytest

from adam.config.settings import ArtifactConfig, ToolLimits
from adam.contracts.errors import StorageError
from adam.session.artifacts import ArtifactStore
from adam.tool_calls.command_tools import CommandTools
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_artifacts import application, reference_from
from tests.features.test_tool_loop import answer, tool_response


def command():
    return [
        sys.executable,
        "-c",
        "import sys; "
        "sys.stdout.buffer.write(('BEGIN-A\\n'+'中间日志\\n'*3000+'END-A').encode()); "
        "sys.stderr.buffer.write(('BEGIN-B\\n'+'warning\\n'*3000+'END-B').encode()); "
        "sys.exit(7)",
    ]


@pytest.mark.parametrize("capacity", [200000, 1500])
def test_model_sees_both_tails_and_attachment_completeness_matches_actual_storage(
    tmp_path, capacity
):
    config = replace(
        config_at(tmp_path),
        tool_limits=ToolLimits(max_result_chars=1024),
        artifacts=ArtifactConfig(max_bytes=capacity),
    )
    approvals = []

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("command", "run_command", argv=command())])
        result = outputs(body)[-1]
        for label in ("A", "B"):
            text = result["stdout" if label == "A" else "stderr"]
            assert text.startswith("BEGIN-" + label) and text.endswith("END-" + label)
        assert result["exit_code"] == 7 and result["status"] == "completed"
        assert result["truncated"] and result["source_truncated"] == (capacity == 1500)
        assert result["artifact"]["complete"] == (capacity == 200000)
        assert all(value > 0 for value in result["preview_omitted_chars"].values())
        assert (
            len(next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool"))
            <= 1024
        )
        return answer("last error seen")

    with application(tmp_path, respond, config=config, approvals=approvals) as (app, store, _):
        assert app.send("inspect output") == "last error seen"
        sid = app.session.session_id
        ref = reference_from(store, sid)
        page = ArtifactStore(
            store.root / "_artifacts", max_bytes=capacity, max_read_chars=100000
        ).read(sid, ref.artifact_id, expected_version=ref.version, count=100000)
        assert ref.saved_bytes <= capacity and page.next_offset is None
        assert ref.complete == (capacity == 200000)
        if ref.complete:
            raw = json.loads(page.text)
            assert raw["stdout"] == "BEGIN-A\n" + "中间日志\n" * 3000 + "END-A"
            assert raw["stderr"] == "BEGIN-B\n" + "warning\n" * 3000 + "END-B"
            assert raw["output_capture"]["stdout"]["omitted_bytes"] == 0
        else:
            assert ref.total_bytes is None
        execution = store.load_executions(sid)[0]
        assert execution["stdout"].endswith("END-A") and execution["stderr"].endswith("END-B")
        assert len(approvals) == 1


@pytest.mark.parametrize("limit", [512, 1024, 4096])
def test_without_artifacts_final_json_is_bounded_and_keeps_tail_without_mutating_result(
    tmp_path, limit
):
    tools = CommandTools(tmp_path, ToolLimits(max_result_chars=limit), lambda _: True)
    result = tools.run_command({"argv": command()})
    original = deepcopy(result)
    encoded = tools.encode_result(result)
    preview = json.loads(encoded)
    assert len(encoded) <= limit and result == original
    assert preview["exit_code"] == 7 and preview["truncated"]
    assert preview["output_capture"] == result["output_capture"]
    assert preview["stdout"].endswith("END-A") and preview["stderr"].endswith("END-B")
    for stream, omitted in preview["preview_omitted_chars"].items():
        head, rest = preview[stream].split("\n[...预览省略 ")
        count, tail = rest.split(" 字符...]\n")
        assert int(count) == omitted == len(result[stream]) - len(head) - len(tail)


def test_failed_attachment_keeps_tail_and_does_not_repeat_the_actual_command(tmp_path, monkeypatch):
    approvals = []

    def fail(*args, **kwargs):
        raise StorageError("injected artifact write boundary")

    monkeypatch.setattr(ArtifactStore, "save", fail)

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("command", "run_command", argv=command())])
        result = outputs(body)[-1]
        assert result["artifact_unavailable"] and "artifact" not in result
        assert result["stdout"].endswith("END-A") and result["stderr"].endswith("END-B")
        assert result["exit_code"] == 7 and not result["source_truncated"]
        return answer("evidence incomplete")

    with application(tmp_path, respond, approvals=approvals) as (app, store, requests):
        app.send("inspect output")
        assert len(approvals) == 1 and len(requests) == 2
        assert len(store.load_executions(app.session.session_id)) == 1


def test_escaped_unicode_preview_counts_characters_and_preserves_short_stream(tmp_path):
    tools = CommandTools(tmp_path, ToolLimits(max_result_chars=512))
    text = "BEGIN" + '"\\\n\t汉😀' * 1000 + "END"
    result = {
        "ok": True,
        "status": "completed",
        "exit_code": 9,
        "stdout": text,
        "stderr": "error",
        "truncated": False,
    }
    encoded = tools.encode_result(result)
    value = json.loads(encoded)
    assert len(encoded) <= 512 and value["stdout"].startswith("BEGIN")
    assert value["stdout"].endswith("END") and value["stderr"] == "error"
    assert "\ufffd" not in value["stdout"] and result["stdout"] == text
    assert value["exit_code"] == 9 and value["preview_omitted_chars"]["stdout"] > 0
