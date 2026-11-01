"""B06.3：固定题集留证和主子继承验收；模拟向量只用于检查工程边界。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import adam.bootstrap as bootstrap
import scripts.memory_eval as memory_eval
from adam.config.settings import MemoryConfig
from adam.contracts.errors import MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemoryReference, MemorySubject, validate_selection
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at, memory_records
from tests.features.test_semantic_memory import LocalVectors
from tests.features.test_subagents import application, call, index_for, outputs
from tests.features.test_tool_loop import answer, tool_response


class UniformVectors:
    """刻意让所有资料同分；可揭示误召回，不能据此声称语义检索准确。"""

    identity = "fixture:uniform:v1"

    def passages(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def query(self, text):
        return [1.0, 0.0]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixed_evaluation_keeps_every_case_and_actual_source_versions(tmp_path):
    root = tmp_path / "evaluation"
    report = memory_eval.evaluate(root, tmp_path / "cache", backend=UniformVectors())
    manifest = read_json(root / "manifest.json")
    assert report == read_json(root / "report.json") and report["backend"] == "injected"
    assert len(report["rows"]) == 20 and len(manifest["cases"]) == 10
    sources = {r["memory_id"]: r for r in manifest["sources"].values()}
    for number, case in enumerate(memory_eval.CASES, 1):
        for method in ("keyword", "semantic"):
            row = read_json(root / f"{number:02d}-{method}.json")
            assert row in report["rows"] and row["case_id"] == case.case_id
            assert not row["forbidden_selected"]
            validate_selection(row["selection"])
            assert all(r == sources[r["memory_id"]] for r in row["selection"]["records"])
    for group in report["groups"].values():
        assert group["positive_cases"] == 6 and group["empty_cases"] == 4
        assert group["forbidden_selected"] == group["unavailable_cases"] == 0
    ambiguous = [r for r in report["rows"] if r["case_id"] == "ambiguous-name"]
    assert all(r["correct_empty"] and r["ambiguity_notice"] for r in ambiguous)
    conflict = [r for r in report["rows"] if r["case_id"] == "conflicting-current"]
    assert all(set(r["selected_aliases"]) == {"rolling", "blue-green"} for r in conflict)
    assert manifest["sources"]["language-old"]["revision"] == 2
    assert manifest["sources"]["language-new"]["supersedes"]["revision"] == 1
    assert not (tmp_path / "cache").exists()
    before = (root / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        memory_eval.evaluate(root, tmp_path / "cache", backend=UniformVectors())
    assert (root / "report.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["backend", "storage"])
def test_evaluation_does_not_report_degraded_or_unavailable_as_semantic_success(
    tmp_path, monkeypatch, fault
):
    def fail(*args, **kwargs):
        if fault == "storage":
            raise MemoryStoreError("injected read fault")
        raise RuntimeError("private model failure")

    backend = UniformVectors()
    if fault == "backend":
        backend.passages = fail
    else:
        monkeypatch.setattr(memory_eval.MemoryStore, "list_records", fail)
    report = memory_eval.evaluate(tmp_path / "run", tmp_path / "cache", backend=backend)
    semantic = report["groups"]["semantic"]
    if fault == "backend":
        assert semantic["fallback_cases"] == 9 and semantic["unavailable_cases"] == 0
        assert "private model failure" not in json.dumps(report)
    else:
        assert semantic["unavailable_cases"] == 10
        assert semantic["correct_empty_cases"] == semantic["all_expected_cases"] == 0
        assert semantic["recall_at_3"] == 0


def test_failed_evidence_write_stops_before_next_query_and_never_outputs_summary(
    tmp_path, monkeypatch
):
    root = tmp_path / "run"
    original = memory_eval.write_record
    queries = []
    backend = UniformVectors()
    backend.query = lambda text: queries.append(text) or [1.0, 0.0]

    def fail_second_record(path, data, **kwargs):
        # 模拟实际选择已完成、证据落盘失败；不得继续下一题或输出完整汇总。
        if path.name == "01-semantic.json":
            raise OSError("injected evidence save failure")
        original(path, data, **kwargs)

    monkeypatch.setattr(memory_eval, "write_record", fail_second_record)
    with pytest.raises(OSError):
        memory_eval.evaluate(root, tmp_path / "cache", backend=backend)
    assert queries == [memory_eval.CASES[0].query]
    assert (root / "manifest.json").exists() and (root / "01-keyword.json").exists()
    assert not (root / "02-semantic.json").exists() and not (root / "report.json").exists()


def test_cli_uses_explicit_local_cache_without_loading_chat_connection(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def factory(cache, model):
        calls.append((cache, model))
        return UniformVectors()

    monkeypatch.setattr(memory_eval, "FastEmbedBackend", factory)
    root, cache = tmp_path / "run", tmp_path / "models"
    arguments = [str(root), "--model-cache", str(cache), "--model", "fixture-local"]
    assert memory_eval.main(arguments) == 0
    assert calls == [(cache / "models", "fixture-local")]
    assert json.loads(capsys.readouterr().out)["requested_model"] == "fixture-local"
    assert memory_eval.main(arguments) == 1
    assert "未完成" in capsys.readouterr().err and len(calls) == 1


def test_semantic_subject_snapshot_is_frozen_for_child_and_refreshes_on_next_parent_turn(
    tmp_path, monkeypatch
):
    backend = LocalVectors()
    monkeypatch.setattr(bootstrap, "FastEmbedBackend", lambda *args: backend)
    config = replace(config_at(tmp_path), memory=MemoryConfig(method="semantic"))
    parent_bodies, child_bodies = [], []
    changed = []

    def parent(request):
        body = json.loads(request.content)
        parent_bodies.append(body)
        step = len(parent_bodies)
        if step == 1:
            # 当前轮次已经选择旧偏好；替代发生在委派前，子任务仍须继承本轮证据。
            changed.extend(
                app.manage_memory(
                    MemoryCommand(
                        "add", "改完程序再做单元测试", supersedes=MemoryReference(item.memory_id, 1)
                    )
                )
            )
            result = tool_response([call("spawn", "spawn_agent", name="helper", task="审阅代码")])
        elif step == 4:
            result = tool_response([call("follow", "send_message", agent_id=child_id, task="继续")])
        elif step in {2, 5}:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(body)[-1]["agents"][0]["status"] == "completed"
            result = answer("已核对来源")
        return httpx.Response(200, json=result)

    def child(request):
        child_bodies.append(json.loads(request.content))
        return httpx.Response(200, json=answer("已读取主任务资料"))

    with application(tmp_path, parent, child, config=config) as (app, store):
        item = app.manage_memory(
            MemoryCommand("add", "改动前先做单元测试", subject=MemorySubject("owner", "维护者"))
        )[0]
        query = "@owner 怎样检验程序？"
        assert app.send(query) == "已核对来源"
        child_id = read_json(index_for(app))["agents"][0]["agent_id"]
        assert app.send(query) == "已核对来源"
        assert memory_records(child_bodies[0]) == [item.to_dict()]
        assert memory_records(child_bodies[1]) == [changed[0].to_dict()]
        assert backend.queries_seen == [query, query]
        assert backend.passages_seen == [item.content, changed[0].content]
        parent_id = app.session.session_id
        parent_evidence = [
            e["payload"]["context"]["memory"]
            for e in store.load_events(parent_id)
            if e["type"] == "request_started"
        ]
        with SessionStore(store.root / "_subagents" / parent_id / "sessions") as children:
            children.select_session(child_id)
            child_evidence = [
                e["payload"]["context"]["memory"]
                for e in children.load_events(child_id)
                if e["type"] == "request_started"
            ]
        assert child_evidence == [parent_evidence[0], parent_evidence[3]]
        assert all(r["strategy"] == "semantic_v1" for r in child_evidence)
        assert child_evidence[1]["records"][0]["supersedes"]["memory_id"] == item.memory_id


def test_saved_local_model_evaluation_preserves_misses_and_traceable_versions():
    root = Path(__file__).resolve().parents[2]
    evidence = read_json(root / "docs" / "memory-retrieval-evidence.json")
    manifest, report = evidence["manifest"], evidence["report"]
    assert manifest["backend"] == report["backend"] == "local_model"
    assert len(report["rows"]) == 20
    sources = {r["memory_id"]: r for r in manifest["sources"].values()}
    for row in report["rows"]:
        validate_selection(row["selection"])
        assert not row["forbidden_selected"]
        assert all(r == sources[r["memory_id"]] for r in row["selection"]["records"])
    missed = [r for r in report["rows"] if r["case_id"] == "paraphrase"]
    assert len(missed) == 2 and all(not r["all_expected"] for r in missed)
    assert report["groups"]["keyword"]["all_expected_cases"] == 4
    assert report["groups"]["semantic"]["all_expected_cases"] == 5
    assert report["groups"]["semantic"]["correct_empty_cases"] == 3
    assert evidence["model"]["snapshot"] == "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
