"""B08.3：真实会话变化与恢复边界，不把缓存、失败输入或摘要误报为当前原文。"""

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from adam.config.settings import SessionSearchConfig
from adam.session.search import SessionSearchIndex
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_session_search import seed
from tests.features.test_session_search_tools import read_call
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_artifacts import application
from tests.features.test_tool_loop import answer, tool_response


@pytest.mark.parametrize("change", ["edited", "deleted", "corrupted", "denied"])
def test_live_tool_rejects_stale_reference_and_never_reuses_invalid_cached_source(
    tmp_path, monkeypatch, change
):
    config = config_at(tmp_path)
    sid, _, path = seed(config.sessions_dir, "OLD_EVIDENCE original source")
    healthy, _, _ = seed(config.sessions_dir, "OLD_EVIDENCE healthy source")
    config = replace(config, session_search=SessionSearchConfig(allowed_session_ids=(sid, healthy)))
    original_open = Path.open
    original_bytes = path.read_bytes()

    def respond(body, number):
        if number == 1:
            return tool_response([call("search", "search_sessions", query="OLD_EVIDENCE")])
        if number == 2:
            hit = next(hit for hit in outputs(body)[-1]["hits"] if hit["session_id"] == sid)
            if change == "edited":
                records = [json.loads(line) for line in original_bytes.splitlines()]
                records[hit["line"] - 1]["payload"]["message"]["content"] = (
                    "NEW_EVIDENCE revised source"
                )
                path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            elif change == "deleted":
                path.unlink()
            elif change == "corrupted":
                path.write_bytes(original_bytes + b"{half-written-tail")
            else:

                def deny(target, *args, **kwargs):
                    # 模拟另一个来源的读取权限被撤销，不改变当前会话日志的写入权限。
                    if target == path and args and args[0] == "rb":
                        raise PermissionError("injected source access revocation")
                    return original_open(target, *args, **kwargs)

                monkeypatch.setattr(Path, "open", deny)
            return tool_response([read_call(hit)])
        if number == 3:
            assert not outputs(body)[-1]["ok"]
            return tool_response([call("refresh", "search_sessions", query="OLD_EVIDENCE")])
        result = outputs(body)[-1]
        assert {hit["session_id"] for hit in result["hits"]} == {healthy}
        assert result["complete"] is (change == "edited")
        if change != "edited":
            assert result["issues"][0]["session_id"] == sid
        return answer("old reference unavailable; healthy source preserved")

    with application(tmp_path, respond, config=config) as (app, _, requests):
        assert app.send("inspect authorized historical evidence").startswith(
            "old reference unavailable"
        )
        assert len(requests) == 4
    monkeypatch.setattr(Path, "open", original_open)
    if change == "corrupted":
        assert path.read_bytes() == original_bytes + b"{half-written-tail"
        assert not list(path.parent.glob("*.bak*"))
    elif change == "denied":
        assert path.read_bytes() == original_bytes


def test_append_during_snapshot_is_detected_and_next_explicit_query_sees_new_commit(
    tmp_path, monkeypatch
):
    config = config_at(tmp_path)
    sid, _, path = seed(config.sessions_dir, "RACE_OLD original")
    config = replace(config, session_search=SessionSearchConfig(allowed_session_ids=(sid,)))
    original = Path.open
    changed = False

    @contextmanager
    def raced_open(target, *args, **kwargs):
        nonlocal changed
        with original(target, *args, **kwargs) as stream:
            yield stream
            if target == path and args and args[0] == "rb" and not changed:
                # 在真实读取完成、路径版本复查之前追加完整轮次；只控制竞争发生时刻。
                changed = True
                with SessionStore(config.sessions_dir) as writer:
                    writer.append_turn(sid, "later", "RACE_NEW committed after snapshot")

    monkeypatch.setattr(Path, "open", raced_open)

    def respond(body, number):
        if number == 1:
            return tool_response([call("first", "search_sessions", query="RACE_OLD")])
        if number == 2:
            result = outputs(body)[-1]
            assert not result["hits"] and result["complete"] is False
            assert result["issues"] == [{"session_id": sid, "code": "source_changing"}]
            return tool_response([call("retry", "search_sessions", query="RACE_NEW")])
        result = outputs(body)[-1]
        assert result["complete"] and result["hits"][0]["text"].endswith("after snapshot")
        return answer("fresh committed source")

    with application(tmp_path, respond, config=config) as (app, _, requests):
        assert app.send("inspect changing source") == "fresh committed source"
        assert len(requests) == 3 and changed


def test_reopen_after_compaction_keeps_originals_and_separates_interrupted_input(tmp_path):
    config = config_at(tmp_path)
    original = "ARCHIVE_ANCHOR 原约束：发布必须确认。"
    pending_text = "INTERRUPTED_ANCHOR 未完成，不能假装已发布。"

    with application(tmp_path, lambda *_: answer("简短摘要省略具体约束"), config=config) as (
        app,
        store,
        _,
    ):
        sid = app.new().session_id
        for number in range(5):
            store.append_turn(sid, f"old question {number}", original if number == 0 else "other")
        app.resume(sid)
        summary = app.compact()
        pending = store.start_turn(sid, pending_text)
        source = next(config.sessions_dir.glob(f"*/*/*/{sid}.jsonl"))
        before = source.read_bytes()
        # 只读索引不替主会话恢复；在途输入只能以诊断标签查看。
        index = SessionSearchIndex(config.sessions_dir, (sid,))
        assert not index.search("INTERRUPTED_ANCHOR").hits
        assert (
            index.search("INTERRUPTED_ANCHOR", include_diagnostics=True).hits[0].status
            == "in_progress"
        )
        assert source.read_bytes() == before

    def respond(body, number):
        if number == 1:
            assert original not in json.dumps(body["messages"], ensure_ascii=False)
            assert pending_text not in json.dumps(body["messages"], ensure_ascii=False)
            return tool_response([call("archive", "search_sessions", query="ARCHIVE_ANCHOR")])
        if number == 2:
            hit = outputs(body)[-1]["hits"][0]
            assert hit["text"] == original and hit["turn_id"] in summary.covered_turn_ids
            return tool_response(
                [
                    call(
                        "diagnostic",
                        "search_sessions",
                        query="INTERRUPTED_ANCHOR",
                        include_diagnostics=True,
                    )
                ]
            )
        hit = outputs(body)[-1]["hits"][0]
        assert hit["turn_id"] == pending and hit["status"] == "interrupted"
        assert hit["kind"] == "diagnostic_input" and hit["message_index"] is None
        assert all(
            pending_text != m.get("content")
            for m in body["messages"]
            if m["role"] in {"user", "assistant"}
        )
        return answer("recovered original; interrupted task remains incomplete")

    with application(tmp_path, respond, config=config) as (app, store, _):
        app.resume(sid)
        assert store.execution_report(sid, pending).status == "interrupted"
        assert app.send("check archived facts").startswith("recovered original")
        assert len(store.load_turns(sid)) == 6


def test_authorization_removed_on_restart_never_queries_previously_allowed_session(
    tmp_path, monkeypatch
):
    config = config_at(tmp_path)
    sid, _, path = seed(config.sessions_dir, "EXTRA_SCOPE privately authorized")
    allowed = replace(config, session_search=SessionSearchConfig(allowed_session_ids=(sid,)))

    def respond(body, number):
        if number == 1:
            return tool_response([call("search", "search_sessions", query="EXTRA_SCOPE")])
        return answer("checked")

    with application(tmp_path, respond, config=allowed) as (app, _, requests):
        app.send("find authorized source")
        current = app.session.session_id
        assert outputs(requests[-1])[-1]["hits"][0]["session_id"] == sid
    original = Path.open

    def deny(target, *args, **kwargs):
        if target == path:
            pytest.fail("removed authorization must not open old source")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    with application(tmp_path, respond, config=config) as (app, _, requests):
        # 新会话避免已合法返回的旧工具资料自身形成关键词命中。
        app.new()
        assert app.session.session_id != current
        app.send("find source after removing authorization")
        assert outputs(requests[-1])[-1]["hits"] == []


def test_real_directory_link_cannot_turn_authorized_id_into_external_file(tmp_path):
    outside = tmp_path / "outside"
    sid, _, path = seed(outside, "EXTERNAL_SOURCE not in the authorized root")
    root = tmp_path / "sessions"
    root.mkdir()
    year = path.relative_to(outside).parts[0]
    link = root / year
    try:
        link.symlink_to(outside / year, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"New-Item -ItemType Junction -Path '{year}' -Target '..\\outside\\{year}' | Out-Null",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        result = SessionSearchIndex(root, (sid,)).search("EXTERNAL_SOURCE")
        assert not result.hits and result.issues[0].code == "outside_root"
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()  # Windows junction 本身是空目录入口，不递归删除目标。
    assert path.exists()
