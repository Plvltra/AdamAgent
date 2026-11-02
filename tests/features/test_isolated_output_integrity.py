"""B19.3.1：后端解码损失必须传到附件与会话；实际字节解码另由真实 WSL 验收。"""

import json
from copy import deepcopy

import httpx
import pytest

from adam.config.settings import ToolLimits
from adam.contracts.command_execution import CommandIsolationResult
from adam.contracts.execution import ISOLATED_COMMAND_TOOL
from adam.isolation.commands import IsolatedCommands
from adam.recording.runtime import replay_turn
from tests.features.test_isolated_command_flow import enabled, projected
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions


@pytest.mark.parametrize("loss", [False, True])
def test_encoding_fact_controls_attachment_completeness_and_survives_replay(
    tmp_path, monkeypatch, loss
):
    """同样的替换字符可来自合法文本或丢失字节；上层只使用可信后端的明确标记。"""

    def run(self, argv, **kwargs):
        return CommandIsolationResult(
            0, "exited", "�" + "x" * 6000, "", 6003, 20, invalid_utf8=loss
        )

    monkeypatch.setattr(IsolatedCommands, "run", run)

    def respond(body, number):
        if number == 1:
            value = tool_response(
                [call("command", ISOLATED_COMMAND_TOOL, argv=["cat", "input.bin"])]
            )
        else:
            result = outputs(body)[-1]
            assert result["exit_code"] == 0 and result["source_truncated"] is loss
            assert result["artifact"]["complete"] is (not loss)
            value = answer("output may be incomplete" if loss else "complete text")
        return httpx.Response(200, json=value)

    config = enabled(tmp_path, tool_limits=ToolLimits(max_result_chars=1000))
    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, path, _):
        recorder.run(lambda: app.send("task"))
        events = store.load_events(app.session.session_id)
        result = store.load_executions(app.session.session_id)[0]
        assert result["invalid_utf8"] is loss
        output = next(
            json.loads(e["payload"]["output"]) for e in events if e["type"] == "tool_finished"
        )
        reference = store.load_artifact(app.session.session_id, output["artifact"]["artifact_id"])
        assert reference is not None and reference.complete is (not loss)
        if loss:
            assert result["truncated"]
            altered = deepcopy(events)
            finish = next(e["payload"] for e in altered if e["type"] == "command_finished")
            finish["truncated"] = False
            with pytest.raises(ValueError, match="decoding loss"):
                projected(altered)
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        IsolatedCommands, "run", lambda *args, **kwargs: pytest.fail("replay executed backend")
    )
    assert replay_turn(path, tmp_path / "replay")["status"] == "matched"
