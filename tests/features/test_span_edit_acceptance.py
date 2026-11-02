"""B23.3：固定三百行实际编辑对照与原应用闭环，不将参数字节当token或模型质量。"""

import json

import httpx

from adam.tool_calls.registry import ToolRegistry
from scripts.span_edit_eval import AFTER, BEFORE, BLOCK, END, REPLACEMENT, START, evaluate, main
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app


def test_fixed_comparison_writes_same_result_and_keeps_complete_evidence(tmp_path):
    directory = tmp_path / "run"
    result = evaluate(directory)
    assert result["passed"] and result["same_result"]
    assert result["selected_lines"] == 300 and result["source_lines"] == 302
    original, span = result["rows"]
    assert span["arguments_utf8_bytes"] < original["arguments_utf8_bytes"] / 10
    assert span["approved_diff_chars"] > span["arguments_chars"] * 10
    assert (
        result["saved_argument_bytes"]
        == original["arguments_utf8_bytes"] - span["arguments_utf8_bytes"]
    )
    assert json.loads((directory / "report.json").read_text(encoding="utf-8")) == result
    for row in result["rows"]:
        saved = json.loads((directory / (row["tool"] + ".json")).read_text(encoding="utf-8"))
        encoded = json.dumps(saved["arguments"], ensure_ascii=False, separators=(",", ":"))
        assert saved["result"] == row and len(encoded.encode()) == row["arguments_utf8_bytes"]
        assert (directory / row["tool"] / "sample.py").read_bytes() == (
            BEFORE + REPLACEMENT + AFTER
        ).encode()


def test_existing_comparison_directory_is_not_replaced(tmp_path, capsys):
    marker = tmp_path / "preserved"
    marker.write_bytes(b"original")
    assert main([str(tmp_path)]) == 1
    assert marker.read_bytes() == b"original" and list(tmp_path.iterdir()) == [marker]
    assert "未完成" in capsys.readouterr().err


def test_failed_edit_stays_in_report_instead_of_false_success(tmp_path, monkeypatch, capsys):
    execute = ToolRegistry.execute

    def refuse(registry, name, *args, **kwargs):
        if name == "replace_span":
            return '{"ok":false,"status":"denied"}'
        return execute(registry, name, *args, **kwargs)

    monkeypatch.setattr(ToolRegistry, "execute", refuse)
    directory = tmp_path / "run"
    assert main([str(directory)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False and not report["same_result"]
    assert report["rows"][0]["matches_expected"]
    assert report["rows"][1]["status"] == "denied" and report["rows"][1]["approvals"] == 0
    assert (directory / "replace_span/sample.py").read_bytes() == (BEFORE + BLOCK + AFTER).encode()


def test_model_reads_distant_boundaries_then_edits_three_hundred_lines_and_undoes(tmp_path):
    target = tmp_path / "sample.py"
    raw = (BEFORE + BLOCK + AFTER).encode()
    target.write_bytes(raw)

    def respond(body, number):
        if number == 1:
            value = tool_response([call("first", "read_file", path="sample.py", max_lines=3)])
        elif number == 2:
            value = tool_response(
                [call("last", "read_file", path="sample.py", start_line=300, max_lines=3)]
            )
        elif number == 3:
            results = outputs(body)
            assert results[0]["version"] == results[1]["version"]
            value = tool_response(
                [
                    call(
                        "span",
                        "replace_span",
                        path="sample.py",
                        start_text=START,
                        end_text=END,
                        new_text=REPLACEMENT,
                        expected_version=results[-1]["version"],
                    )
                ]
            )
        else:
            assert outputs(body)[-1]["status"] == "applied"
            value = answer("changed 300 lines")
        return httpx.Response(200, json=value)

    with captured_app(tmp_path, respond) as (app, recorder, store, _, bodies):
        assert recorder.run(lambda: app.send("task")) == "changed 300 lines"
        assert target.read_bytes() == (BEFORE + REPLACEMENT + AFTER).encode()
        edit = store.load_edits(app.session.session_id)[0]
        checkpoint = store.load_edit_checkpoint(
            app.session.session_id, edit["edit_id"], max_bytes=100000
        )
        assert checkpoint.original == raw and len(bodies) == 4
        assert app.undo().status == "applied" and target.read_bytes() == raw
