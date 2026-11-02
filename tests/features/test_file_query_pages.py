"""B21.1.2：真实文件工具稳定续读；模拟模型只负责申请下一页，本地IO和来源记录真实。"""

import hashlib
import json
import os
from dataclasses import replace

import httpx
import pytest

from adam.config.loader import load_config
from adam.config.settings import PermissionConfig, ToolLimits
from adam.contracts.errors import ConfigError
from adam.recording.runtime import replay_turn
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.file_queries import FileQueries
from tests.features.test_memory_context import config_at
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions
from tests.test_config import LLM
from tests.test_tools import run


@pytest.mark.parametrize("kind", ["list_directory", "find_files", "search_text"])
def test_all_pages_are_stable_complete_and_bounded_without_duplicates(tmp_path, kind):
    for number in reversed(range(61)):
        (tmp_path / f"{number:03d}.txt").write_text("needle\n", encoding="utf-8")
    tools = create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=8000))
    args = {"path": ".", "limit": 7}
    if kind == "find_files":
        args["pattern"] = "*.txt"
    if kind == "search_text":
        args["query"] = "needle"
    found, generations = [], set()
    for _ in range(15):
        encoded = tools.execute(kind, json.dumps(args))
        assert len(encoded) <= 8000
        page = json.loads(encoded)
        assert page["ok"] and "preview" not in page
        items = page["entries" if kind == "list_directory" else "results"]
        assert 0 < len(items) <= 7
        found.extend(item["path"] if isinstance(item, dict) else item for item in items)
        generations.add(page["generation"])
        assert page["truncated"] == (page["next_cursor"] is not None)
        if not page["next_cursor"]:
            break
        args["cursor"] = page["next_cursor"]
    assert found == [f"{n:03d}.txt" for n in range(61)]
    assert len(generations) == 1


def test_small_result_budget_keeps_whole_items_and_cursor(tmp_path):
    for i in range(20):
        (tmp_path / f"{i:02d}.txt").write_text("needle")
    tools = create_tool_registry(tmp_path, limits=ToolLimits(max_result_chars=512))
    first = run(tools, "find_files", path=".", pattern="*")
    assert first["next_cursor"] and first["results"] and "preview" not in first
    second = run(tools, "find_files", path=".", pattern="*", cursor=first["next_cursor"])
    assert first["results"][-1] < second["results"][0]
    # 即使转义扩大JSON，不能用残缺条目或丢失游标的preview冒充页面。
    (tmp_path / "long.txt").write_text("needle" + '"' * 500)
    result = run(tools, "search_text", path="long.txt", query="needle")
    assert not result["ok"] and "单个搜索条目" in result["error"]


def test_cursor_rejects_changed_query_and_changed_bytes_with_restored_mtime(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("needle a\nneedle b\n", encoding="utf-8")
    tools = create_tool_registry(tmp_path)
    first = run(tools, "search_text", path="sample.txt", query="needle", limit=1)
    cursor = first["next_cursor"]
    assert first["results"][0]["version"] == hashlib.sha256(target.read_bytes()).hexdigest()
    changed_query = run(tools, "search_text", path="sample.txt", query="other", cursor=cursor)
    assert not changed_query["ok"] and "不属于" in changed_query["error"]
    before = target.stat()
    target.write_text("needle c\nneedle d\n", encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed_bytes = run(tools, "search_text", path="sample.txt", query="needle", cursor=cursor)
    assert not changed_bytes["ok"] and "来源已变化" in changed_bytes["error"]
    assert "results" not in changed_bytes


def test_cursor_never_grants_access_after_permission_is_revoked(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "a.txt").write_text("private")
    (outside / "b.txt").write_text("private")
    allowed = create_tool_registry(workspace, PermissionConfig(additional_read_dirs=(outside,)))
    page = run(allowed, "find_files", path=str(outside), pattern="*", limit=1)
    denied = run(
        create_tool_registry(workspace),
        "find_files",
        path=str(outside),
        pattern="*",
        cursor=page["next_cursor"],
    )
    assert not denied["ok"] and "private" not in str(denied)


def test_skipped_files_are_explicit_and_total_byte_limit_is_not_partial_success(tmp_path):
    (tmp_path / "a.txt").write_text("needle")
    (tmp_path / "b.bin").write_bytes(b"\xff\x00")
    (tmp_path / "c.txt").write_bytes(b"x" * 30)
    registry = create_tool_registry(tmp_path, limits=ToolLimits(max_text_file_bytes=20))
    result = run(registry, "search_text", path=".", query="needle")
    assert result["ok"] and result["skipped_files"] == 2
    assert [r["path"] for r in result["results"]] == ["a.txt"]
    limited = create_tool_registry(tmp_path, limits=ToolLimits(max_scan_bytes=7))
    failure = run(limited, "search_text", path=".", query="needle")
    assert not failure["ok"] and "总读取字节" in failure["error"]
    assert "results" not in failure


def test_scan_rechecks_directory_after_real_file_change(tmp_path, monkeypatch):
    import adam.tool_calls.file_queries as queries

    target = tmp_path / "sample.txt"
    target.write_text("needle")
    original = queries.read_snapshot

    def change_after_read(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        (tmp_path / "new.txt").write_text("needle")
        return snapshot

    monkeypatch.setattr(queries, "read_snapshot", change_after_read)
    result = run(create_tool_registry(tmp_path), "search_text", path=".", query="needle")
    assert not result["ok"] and "扫描期间" in result["error"]


@pytest.mark.parametrize("value", [0, -1, True, 100000001])
def test_total_scan_limit_cannot_be_disabled(value):
    with pytest.raises(ConfigError):
        ToolLimits(max_scan_bytes=value)


def test_scan_budget_loads_from_toml_and_rejects_invalid_type(tmp_path):
    config = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    config.write_text("[tool_limits]\nmax_scan_bytes=4096", encoding="utf-8")
    assert load_config(config).tool_limits.max_scan_bytes == 4096
    config.write_text("[tool_limits]\nmax_scan_bytes=true", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config)


def test_model_gets_51st_hit_and_replay_does_not_scan_changed_source(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    target = workspace / "sample.txt"
    target.write_text("".join(f"needle {n}\n" for n in range(1, 62)), encoding="utf-8")
    config = replace(config_at(tmp_path), tool_limits=ToolLimits(max_result_chars=16000))
    pages = []

    def respond(body, phase):
        if phase == 1:
            result = tool_response([call("search", "search_text", path="project", query="needle")])
        elif phase == 2:
            pages.append(outputs(body)[-1])
            assert len(pages[0]["results"]) == 50
            result = tool_response(
                [
                    call(
                        "next",
                        "search_text",
                        path="project",
                        query="needle",
                        cursor=pages[0]["next_cursor"],
                    )
                ]
            )
        else:
            pages.append(outputs(body)[-1])
            assert pages[1]["results"][0]["line"] == 51
            assert pages[1]["next_cursor"] is None
            result = answer("found 61 matches")
        return httpx.Response(200, json=result)

    with captured_app(tmp_path, respond, config=config) as (app, recorder, store, source, bodies):
        assert recorder.run(lambda: app.send("task")) == "found 61 matches"
        events = store.load_events(app.session.session_id)
        finished = [e["payload"] for e in events if e["type"] == "tool_finished"]
        assert len(finished) == 2 and len(bodies) == 3
        assert len({p["generation"] for p in pages}) == 1
        assert store.usage(app.session.session_id) == (36, False)
    before = source.read_bytes()
    target.write_text("different source")

    def no_scan(*_args, **_kwargs):
        pytest.fail("offline replay must not re-run file queries")

    monkeypatch.setattr(FileQueries, "run", no_scan)
    forbid_external_actions(monkeypatch)
    replayed = replay_turn(source, tmp_path / "offline")
    assert replayed["status"] == "matched", replayed
    assert source.read_bytes() == before and target.read_text() == "different source"
