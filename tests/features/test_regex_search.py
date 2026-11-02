"""B21.2.2：显式正则经过真实文件工具、固定进程、模型请求和离线重放。"""

import json
import subprocess
from dataclasses import replace

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import ToolLimits
from adam.contracts.errors import ConfigError
from adam.recording.runtime import replay_turn
from adam.tool_calls.factory import create_tool_registry
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM
from tests.test_tools import run


def test_literal_remains_default_without_process_and_regex_is_explicit(tmp_path, monkeypatch):
    (tmp_path / "values.txt").write_text("value=12\nvalue=ab\n^value=\\d+$\n")
    registry = create_tool_registry(tmp_path)

    def no_process(*args, **kwargs):
        pytest.fail("literal search must not create a regex process")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", no_process)
        literal = run(registry, "search_text", path=".", query=r"^value=\d+$")
    assert [r["line"] for r in literal["results"]] == [3]
    regex = run(registry, "search_text", path=".", query=r"^value=\d+$", mode="regex")
    assert [r["line"] for r in regex["results"]] == [1]
    assert literal["generation"] == regex["generation"]  # 来源相同，查询绑定仍区分模式。
    invalid = run(registry, "search_text", path=".", query="value", mode="auto")
    assert not invalid["ok"] and "mode" in invalid["error"]


def test_regex_pages_keep_source_and_mode_cannot_be_changed(tmp_path):
    target = tmp_path / "values.txt"
    target.write_text("".join(f"value={n}\n" for n in range(61)))
    registry = create_tool_registry(tmp_path)
    params = {"path": ".", "query": r"^value=\d+$", "mode": "regex", "limit": 20}
    first = run(registry, "search_text", **params)
    assert first["ok"] and len(first["results"]) == 20
    wrong_mode = run(
        registry, "search_text", **{**params, "mode": "literal"}, cursor=first["next_cursor"]
    )
    assert not wrong_mode["ok"] and "不属于" in wrong_mode["error"]
    second = run(registry, "search_text", **params, cursor=first["next_cursor"])
    assert second["results"][0]["line"] == 21
    assert first["generation"] == second["generation"]
    target.write_text(target.read_text() + "value=61\n")
    stale = run(registry, "search_text", **params, cursor=second["next_cursor"])
    assert not stale["ok"] and "来源已变化" in stale["error"]


def test_invalid_regex_fails_even_without_matching_files(tmp_path):
    result = run(create_tool_registry(tmp_path), "search_text", path=".", query="[", mode="regex")
    assert not result["ok"] and "正则表达式无效" in result["error"]
    assert "results" not in result


def test_regex_size_bound_does_not_restrict_literal_search(tmp_path):
    (tmp_path / "large.txt").write_text("needle " + "x" * 200)
    registry = create_tool_registry(tmp_path, limits=ToolLimits(max_regex_file_bytes=32))
    rejected = run(registry, "search_text", path=".", query="needle", mode="regex")
    assert not rejected["ok"] and "正则输入" in rejected["error"]
    literal = run(registry, "search_text", path=".", query="needle")
    assert literal["ok"] and len(literal["results"]) == 1


def test_actual_regex_timeout_is_failure_and_following_literal_call_still_works(
    tmp_path, monkeypatch
):
    (tmp_path / "input.txt").write_text("a" * 100000 + "!")
    processes, original = [], subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture)
    registry = create_tool_registry(tmp_path)
    failed = run(registry, "search_text", path=".", query="(a+)+$", mode="regex")
    assert not failed["ok"] and "超时" in failed["error"] and "results" not in failed
    assert len(processes) == 2 and all(process.poll() is not None for process in processes)
    assert run(registry, "search_text", path=".", query="!")["ok"]
    assert len(processes) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_regex_file_bytes", 262145),
        ("regex_timeout_seconds", 0),
        ("regex_timeout_seconds", True),
    ],
)
def test_regex_config_rejects_disabled_or_expanded_worker_bounds(tmp_path, field, value):
    with pytest.raises(ConfigError):
        ToolLimits(**{field: value})
    config = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM)
    config.write_text(f"[tool_limits]\n{field}={str(value).lower()}")
    with pytest.raises(ConfigError):
        load_config(config)


def test_model_regex_result_has_versions_and_replays_without_worker(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "values.txt").write_text("value=10\nvalue=20\nvalue=xx\n")
    config = replace(config_at(tmp_path), tool_limits=ToolLimits(regex_timeout_seconds=2))

    def respond(body, phase):
        if phase == 1:
            result = tool_response(
                [
                    call(
                        "regex",
                        "search_text",
                        path="project",
                        query=r"^value=\d+$",
                        mode="regex",
                        limit=1,
                    )
                ]
            )
        elif phase == 2:
            page = outputs(body)[-1]
            assert [r["line"] for r in page["results"]] == [1]
            result = tool_response(
                [
                    call(
                        "next",
                        "search_text",
                        path="project",
                        query=r"^value=\d+$",
                        mode="regex",
                        cursor=page["next_cursor"],
                    )
                ]
            )
        else:
            page = outputs(body)[-1]
            assert [r["line"] for r in page["results"]] == [2]
            assert len(page["results"][0]["version"]) == 64 and page["next_cursor"] is None
            result = answer("two numeric values")
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "two numeric values"
        finished = [
            e for e in store.load_events(app.session.session_id) if e["type"] == "tool_finished"
        ]
        assert len(finished) == 2 and len(bodies) == 3
        saved_calls = [
            tool
            for message in store.load_messages(app.session.session_id)
            for tool in message.raw.get("tool_calls", [])
        ]
        assert len(saved_calls) == 2
        assert all(
            json.loads(tool["function"]["arguments"])["mode"] == "regex" for tool in saved_calls
        )
    (workspace / "values.txt").write_text("source changed after capture")
    forbid_external_actions(monkeypatch)
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
