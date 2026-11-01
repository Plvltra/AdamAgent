"""B08.1：授权旧对话的只读检索，真实 JSONL 与来源位置，不加载聊天模型。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from adam.contracts.session_search import SessionSearchLimits
from adam.session.outcome import ExecutionOutcome
from adam.session.search import SessionSearchIndex
from adam.session.storage import SessionStore
from adam.session_search import main


def seed(root, text="ORION 保留换行\n禁止自动发布", *, title="source"):
    with SessionStore(root) as store:
        sid = store.create_session(title, "SECRET_SYSTEM 原始系统提示")
        store.append_turn(sid, "ORION 之前的约束是什么", text)
        turn_id = store.load_turns(sid)[0].turn_id
    return sid, turn_id, next(root.glob(f"*/*/*/{sid}.jsonl"))


def read_hit(index, hit, **kwargs):
    return index.read(
        hit.session_id, hit.turn_id, hit.message_index, hit.source_sha256, kind=hit.kind, **kwargs
    )


def test_source_positions_are_exact_and_unauthorized_logs_are_never_opened(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    sid, turn, path = seed(root)
    other, _, denied = seed(root, "ORION PRIVATE_OTHER")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    original = Path.open

    def forbid_unselected(target, *args, **kwargs):
        if target == denied:
            pytest.fail("unauthorized session contents must never be opened")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_unselected)
    index = SessionSearchIndex(root, (sid,))
    result = index.search("禁止自动发布")
    assert not result.issues and len(result.hits) == 1
    hit = result.hits[0]
    assert hit.session_id == sid and hit.turn_id == turn and hit.message_index == 2
    raw = path.read_bytes()
    event = json.loads(raw.splitlines()[hit.line - 1])
    assert event["payload"]["message"]["content"][hit.start : hit.end] == hit.text
    assert hit.snapshot_sha256 == hashlib.sha256(raw).hexdigest()
    assert read_hit(index, hit).text == hit.text
    assert not index.search("SECRET_SYSTEM").hits
    with pytest.raises(ValueError, match="not authorized"):
        index.read(other, turn, 2, hit.source_sha256)
    monkeypatch.setattr(Path, "open", original)
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_appending_does_not_invalidate_source_but_changing_original_requires_new_search(tmp_path):
    sid, _, path = seed(tmp_path)
    index = SessionSearchIndex(tmp_path, (sid,))
    hit = index.search("禁止自动发布").hits[0]
    with SessionStore(tmp_path) as store:
        store.append_turn(sid, "新的问题", "另一段回答")
    fresh = read_hit(index, hit)
    assert fresh.source_sha256 == hit.source_sha256
    assert fresh.snapshot_sha256 != hit.snapshot_sha256
    assert fresh.text == hit.text
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    records[hit.line - 1]["payload"]["message"]["content"] = "已经改变的原始回答"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="changed"):
        read_hit(index, hit)
    assert not index.search("禁止自动发布").hits
    assert index.search("已经改变").hits


@pytest.mark.parametrize("fault", ["deleted", "corrupt", "permission", "duplicate"])
def test_bad_source_drops_cached_results_and_reports_partial_coverage(tmp_path, monkeypatch, fault):
    sid, _, path = seed(tmp_path)
    healthy, _, _ = seed(tmp_path, "ORION healthy evidence")
    index = SessionSearchIndex(tmp_path, (sid, healthy))
    assert {h.session_id for h in index.search("ORION").hits} == {sid, healthy}
    original = Path.open
    if fault == "deleted":
        path.unlink()
    elif fault == "corrupt":
        with path.open("ab") as stream:
            stream.write(b"{half")
    elif fault == "duplicate":
        destination = tmp_path / "2000" / "01" / "01" / path.name
        destination.parent.mkdir(parents=True)
        destination.write_bytes(path.read_bytes())
    else:

        def deny(target, *args, **kwargs):
            if target == path:
                raise PermissionError("injected read denial")
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", deny)
    result = index.search("ORION")
    assert {h.session_id for h in result.hits} == {healthy}
    assert result.to_dict()["complete"] is False and result.issues[0].session_id == sid
    assert (
        result.issues[0].code
        == {
            "deleted": "missing",
            "corrupt": "invalid_log",
            "permission": "read_unavailable",
            "duplicate": "duplicate_id",
        }[fault]
    )


def test_failed_and_unfinished_input_only_appear_as_explicit_diagnostics(tmp_path):
    sid, _, path = seed(tmp_path)
    with SessionStore(tmp_path) as store:
        failed = store.start_turn(sid, "FAILED_INPUT 这轮没有成功")
        store.finish_turn(sid, failed, ExecutionOutcome.failure(ValueError()))
        pending = store.start_turn(sid, "PENDING_INPUT 尚未执行完")
    before = path.read_bytes()
    index = SessionSearchIndex(tmp_path, (sid,))
    assert not index.search("FAILED_INPUT").hits
    hit = index.search("FAILED_INPUT", include_diagnostics=True).hits[0]
    assert hit.kind == "diagnostic_input" and hit.status == "failed" and hit.message_index is None
    assert hit.turn_id == failed and hit.reason == "internal_error"
    assert read_hit(index, hit).text == hit.text
    unfinished = index.search("PENDING_INPUT", include_diagnostics=True).hits[0]
    assert unfinished.turn_id == pending and unfinished.status == "in_progress"
    assert path.read_bytes() == before
    assert not list(tmp_path.rglob("*.bak"))


@pytest.mark.parametrize(
    "field", ["max_file_bytes", "max_total_bytes", "max_scan_entries", "max_messages"]
)
def test_limits_report_incomplete_instead_of_claiming_no_match(tmp_path, field):
    sid, _, _ = seed(tmp_path)
    limits = replace(SessionSearchLimits(), **{field: 1})
    result = SessionSearchIndex(tmp_path, (sid,), limits).search("ORION")
    assert not result.hits and result.issues and result.to_dict()["complete"] is False


def test_long_text_excerpt_is_literal_bounded_and_read_can_continue(tmp_path):
    content = "开头" * 600 + "ORION_MARKER\n真实约束" + "尾部" * 600
    sid, _, _ = seed(tmp_path, content)
    index = SessionSearchIndex(tmp_path, (sid,), SessionSearchLimits(max_excerpt_chars=150))
    hit = index.search("ORION_MARKER").hits[0]
    assert len(hit.text) <= 150 and "ORION_MARKER" in hit.text
    assert content[hit.start : hit.end] == hit.text
    next_part = read_hit(index, hit, start=hit.end, chars=150)
    assert next_part.text == content[hit.end : hit.end + 150]
    with pytest.raises(ValueError):
        read_hit(index, hit, start=-1, chars=20)
    with pytest.raises(ValueError):
        read_hit(index, hit, start=0, chars=151)


def test_missing_root_and_empty_scope_do_not_create_directories(tmp_path):
    root = tmp_path / "missing"
    assert SessionSearchIndex(root, ()).search("query").to_dict()["complete"]
    result = SessionSearchIndex(root, ("1234abcd",)).search("query")
    assert result.issues[0].code == "scan_unavailable" and not root.exists()
    with pytest.raises(ValueError):
        SessionSearchIndex(root, ("../escape",))


def test_path_resolution_outside_root_is_rejected_before_open(tmp_path, monkeypatch):
    sid, _, path = seed(tmp_path / "sessions")
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(path.read_bytes())
    original = Path.resolve

    def resolve(target, *args, **kwargs):
        # 模拟文件系统将这个授权路径解析到目录外；内容必须在打开前拒绝。
        return outside if target == path else original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    result = SessionSearchIndex(path.parents[3], (sid,)).search("ORION")
    assert not result.hits and result.issues[0].code == "outside_root"


def test_standalone_cli_emits_sources_and_failure_status_without_recovery(tmp_path, capsys):
    sid, _, path = seed(tmp_path)
    assert main(["ORION", "--sessions-dir", str(tmp_path), "--session", sid, "--limit", "1"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["has_more"] and len(output["hits"]) == 1
    before = path.read_bytes()
    path.write_bytes(before + b"{half")
    assert main(["ORION", "--sessions-dir", str(tmp_path), "--session", sid]) == 2
    assert json.loads(capsys.readouterr().out)["complete"] is False
    assert path.read_bytes() == before + b"{half"
    assert main(["", "--sessions-dir", str(tmp_path), "--session", sid]) == 1
    assert "没有恢复" in capsys.readouterr().err
