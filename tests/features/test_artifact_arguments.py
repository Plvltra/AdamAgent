"""T8.2 模型参数与关闭配置的业务边界；不让分页参数扩张会话或结果范围。"""

import sys
from dataclasses import replace

import pytest

from adam.config.settings import ArtifactConfig, ToolLimits
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_artifacts import application, populate, read_call
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize(
    "change",
    [{"offset": -1}, {"count": 0}, {"count": 4001}, {"count": True}, {"session_id": "f" * 8}],
)
def test_model_cannot_expand_page_or_select_another_session(tmp_path, change):
    populate(tmp_path)

    def respond(body, phase):
        if phase == 1:
            return tool_response([call("listing", "list_directory", path=".")])
        if phase == 2:
            return tool_response([read_call(outputs(body)[-1]["artifact"], **change)])
        assert outputs(body)[-1]["ok"] is False
        return answer()

    with application(tmp_path, respond) as (app, store, _):
        app.send("invalid page")
        assert len(list((store.root / "_artifacts").glob("*/*.json"))) == 1


def test_disabled_long_command_keeps_bounded_preview_without_attachment(tmp_path):
    config = replace(
        config_at(tmp_path),
        artifacts=ArtifactConfig(enabled=False),
        tool_limits=ToolLimits(max_result_chars=512),
    )

    def respond(body, phase):
        if phase == 1:
            return tool_response(
                [
                    call(
                        "command",
                        "run_command",
                        argv=[sys.executable, "-c", "import sys; sys.stdout.write('x' * 9000)"],
                    )
                ]
            )
        result = outputs(body)[-1]
        assert result["truncated"] and result["exit_code"] == 0
        assert "artifact" not in result and len(result["stdout"]) < 512
        assert len(body["messages"][-1]["content"]) <= 512
        return answer()

    with application(tmp_path, respond, config=config) as (app, store, _):
        app.send("old truncation")
        assert not (store.root / "_artifacts").exists()
