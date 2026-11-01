"""B05.3：模拟 HTTP、生产摘要规划和真实记录，对照流程不执行历史工具或付费请求。"""

import json
import shutil
import subprocess

import httpx
import pytest

import scripts.compaction_eval as evaluation
from adam.config.settings import LLMConfig
from adam.session.storage import SessionStore
from scripts.compaction_eval import main, run_suite
from scripts.compaction_fixture import digest
from scripts.compaction_report import report_suite, review_template
from tests.features.test_compaction_fidelity import CASES
from tests.features.test_streaming import DONE, event, sse
from tests.features.test_tool_loop import answer


def run_at(root, *, handler=None, budget=50000, stream=False):
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if handler:
            return handler(body, len(bodies))
        material = json.loads(body["messages"][1]["content"])
        source = material["turns"][1]["turn_id"]
        text = f"配置文件为 ~/.adam/llm.toml。[{source}:2]"
        if material["current_task"]:
            text += "本次关注：" + material["current_task"]["messages"][0]["content"]
        return httpx.Response(200, json=answer(text))

    result = run_suite(
        root,
        LLMConfig("https://example.test", "test-model", "private-api-key", stream=stream),
        CASES,
        max_known_tokens=budget,
        transport=httpx.MockTransport(respond),
    )
    return result, bodies


def test_two_queries_compare_same_history_and_reports_are_read_only(tmp_path, monkeypatch, capsys):
    def forbid_process(*args, **kwargs):
        raise AssertionError("synthetic history must not execute commands")

    monkeypatch.setattr(subprocess, "Popen", forbid_process)
    root = tmp_path / "run"
    report, bodies = run_at(root)
    assert report["backend"] == "mock" and report["known_tokens"] == 48
    assert not report["usage_unknown"] and len(bodies) == 4
    rows = report["requests"]
    assert all(row["state"] == "succeeded" for row in rows)
    assert [row["arm"] for row in rows] == ["generic", "focused", "focused", "generic"]
    materials = [json.loads(body["messages"][1]["content"]) for body in bodies]
    assert materials[0] == materials[3]
    assert materials[1]["current_task"]["messages"] != materials[2]["current_task"]["messages"]
    assert all(material["turns"] == materials[0]["turns"] for material in materials)
    assert all(body.get("tools") is None for body in bodies)
    assert "保留配置文件路径，不能误写成普通 config.toml" not in json.dumps(
        bodies, ensure_ascii=False
    )
    assert all(row["audit"]["semantic_status"] == "not_reviewed" for row in rows)
    assert all(row["audit"]["references"][0]["resolvable"] for row in rows)
    with SessionStore(root / "sources" / "00") as store:
        sid = store.list_sessions()[0].session_id
        store.select_session(sid)
        assert len(store.load_turns(sid)) == 20 and store.load_summary(sid) is None
        assert store.usage(sid) == (0, False)
    snapshots = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }

    def fail(*args, **kwargs):
        raise AssertionError("report must not request models")

    monkeypatch.setattr(httpx.Client, "send", fail)
    assert main(["report", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["known_tokens"] == 48
    assert main(["review-template", str(root)]) == 0
    template = json.loads(capsys.readouterr().out)
    assert len(template["decisions"]) == 4
    assert all(
        check["verdict"] == "not_reviewed"
        for row in template["decisions"]
        for check in row["checks"]
    )
    assert {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == snapshots
    assert "private-api-key" not in "".join(
        data.decode("utf-8", errors="ignore") for data in snapshots.values()
    )
    assert not (tmp_path / "verify_auth.py").exists()


@pytest.mark.parametrize("failure", ["timeout", "not_sent", "unknown_usage", "invalid", "http"])
def test_failed_or_unknown_request_stops_remaining_comparison(tmp_path, failure):
    def respond(body, number):
        assert number == 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private-api-key")
        if failure == "not_sent":
            raise httpx.ConnectError("private-api-key")
        if failure == "http":
            return httpx.Response(503, text="private-api-key")
        value = answer(" " if failure == "invalid" else "partial result")
        if failure == "unknown_usage":
            del value["usage"]
        return httpx.Response(200, json=value)

    report, bodies = run_at(tmp_path / "run", handler=respond)
    assert len(bodies) == 1 and report["requests"][0]["state"] == "failed"
    assert [row["state"] for row in report["requests"]][1:] == ["planned"] * 3
    assert report["usage_unknown"] == (failure in {"timeout", "unknown_usage", "http"})
    assert report["known_tokens"] == (12 if failure == "invalid" else 0)
    assert "private-api-key" not in json.dumps(report)


def test_cancellation_records_interrupted_and_never_continues(tmp_path):
    def respond(body, number):
        assert number == 1
        raise KeyboardInterrupt()

    root = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run_at(root, handler=respond)
    report = report_suite(root)
    assert report["requests"][0]["state"] == "interrupted"
    assert report["usage_unknown"] and all(
        row["state"] == "planned" for row in report["requests"][1:]
    )


@pytest.mark.parametrize("boundary", ["before_request", "after_response"])
def test_record_write_failure_prevents_untracked_followup_requests(tmp_path, monkeypatch, boundary):
    write = evaluation.write_record
    calls = []

    def fail_write(path, row, **kwargs):
        target = "started" if boundary == "before_request" else "succeeded"
        if row.get("state") == target:
            if boundary == "after_response":
                path.write_text("{", encoding="utf-8")  # 模拟结果覆盖过程中落盘中断。
            raise OSError("injected record failure")
        write(path, row, **kwargs)

    def respond(body, number):
        calls.append(number)
        return httpx.Response(200, json=answer("summary"))

    monkeypatch.setattr(evaluation, "write_record", fail_write)
    root = tmp_path / "run"
    with pytest.raises(OSError):
        run_at(root, handler=respond)
    assert calls == ([] if boundary == "before_request" else [1])
    report = report_suite(root)
    assert report["requests"][0]["state"] == (
        "planned" if boundary == "before_request" else "invalid"
    )
    assert report["usage_unknown"] == (boundary == "after_response")
    assert all(row["state"] == "planned" for row in report["requests"][1:])


def test_budget_checks_before_first_and_after_actual_usage(tmp_path):
    report, bodies = run_at(tmp_path / "no-budget", budget=1)
    assert bodies == [] and report["known_tokens"] == 0 and not report["usage_unknown"]
    assert all(row["state"] == "planned" for row in report["requests"])

    def expensive(body, number):
        assert number == 1
        value = answer("summary")
        value["usage"] = {"prompt_tokens": 50000, "completion_tokens": 1}
        return httpx.Response(200, json=value)

    report, bodies = run_at(tmp_path / "actual-usage", handler=expensive)
    assert len(bodies) == 1 and report["known_tokens"] == 50001
    assert all(row["state"] == "planned" for row in report["requests"][1:])


def test_streaming_summary_records_final_text_and_actual_usage(tmp_path):
    def respond(body, number):
        assert body["stream"] is True
        return sse(
            [
                event({"role": "assistant", "content": "总结"}),
                event({"content": "完成"}),
                event(reason="stop", usage={"prompt_tokens": 7, "completion_tokens": 3}),
                DONE,
            ]
        )

    report, bodies = run_at(tmp_path / "run", handler=respond, stream=True)
    assert len(bodies) == 4 and report["known_tokens"] == 40
    assert all(
        row["summary"] == "总结完成" and row["state"] == "succeeded" for row in report["requests"]
    )


@pytest.fixture
def successful_run(tmp_path):
    root = tmp_path / "run"
    report, _ = run_at(root)
    return root, report


def test_review_records_omissions_without_converting_mock_into_real_quality(
    successful_run, tmp_path
):
    root, report = successful_run
    review = review_template(root)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    blank = report_suite(root, review=path)
    assert blank["groups"]["generic"]["semantics_unreviewed"] == 2
    review["decisions"][0]["checks"][0].update(verdict="preserved", note="人工核对原文路径一致。")
    review["decisions"][2]["checks"][0].update(
        verdict="omitted", note="当前摘要没有保留过期凭据这一原因。"
    )
    path.write_text(json.dumps(review), encoding="utf-8")
    result = report_suite(root, review=path)
    assert result["backend"] == "mock" and result["run_sha256"] == report["run_sha256"]
    assert result["groups"]["focused"]["semantic_verdicts"]["omitted"] == 1
    assert result["groups"]["generic"]["semantic_verdicts"]["preserved"] == 1
    assert result["groups"]["generic"]["semantics_unreviewed"] == 1


@pytest.mark.parametrize(
    "fault", ["run_hash", "summary_hash", "duplicate", "key", "verdict", "reason"]
)
def test_stale_or_invalid_human_review_is_rejected(successful_run, tmp_path, fault):
    root, _ = successful_run
    review = review_template(root)
    decision = review["decisions"][0]
    if fault == "run_hash":
        review["run_sha256"] = "wrong"
    elif fault == "summary_hash":
        decision["summary_sha256"] = "wrong"
    elif fault == "duplicate":
        review["decisions"].append(decision)
    elif fault == "key":
        decision["checks"][0]["key"] = "unknown"
    elif fault == "verdict":
        decision["checks"][0]["verdict"] = "automatic_success"
    else:
        decision["checks"][0]["verdict"] = "preserved"
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ValueError):
        report_suite(root, review=path)


def test_cli_requires_explicit_paid_gate_before_loading_secrets(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("must not load credentials")

    monkeypatch.setattr(evaluation, "load_llm_config", fail)
    with pytest.raises(SystemExit) as error:
        main(["run", str(tmp_path / "new"), "--llm-config", "missing.toml"])
    assert error.value.code == 2 and not (tmp_path / "new").exists()


@pytest.mark.parametrize(
    "fault",
    ["missing", "partial", "wrong_history", "negative_usage", "boolean_index", "bad_messages"],
)
def test_invalid_evidence_is_unknown_and_never_silently_repaired(successful_run, tmp_path, fault):
    original, _ = successful_run
    root = tmp_path / "damaged"
    shutil.copytree(original, root)
    path = root / "01.json"
    row = json.loads(path.read_bytes())
    if fault == "missing":
        path.unlink()
    elif fault == "partial":
        path.write_text("{", encoding="utf-8")
    else:
        if fault == "wrong_history":
            row["source_turn_ids"] = ["invented"]
        elif fault == "negative_usage":
            row["usage"] = [-1, 2]
        elif fault == "boolean_index":
            row["index"] = True
        else:
            row["request"]["messages"] = ["not", "objects"]
        path.write_text(json.dumps(row), encoding="utf-8")
    before = {
        file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()
    }
    result = report_suite(root)
    assert result["requests"][0]["state"] == ("missing" if fault == "missing" else "invalid")
    assert result["known_tokens"] == 36 and result["usage_unknown"]
    assert {
        file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()
    } == before


def test_changed_material_is_rejected_even_if_the_input_hash_is_recomputed(successful_run):
    root, _ = successful_run
    path = root / "01.json"
    row = json.loads(path.read_bytes())
    material = json.loads(row["request"]["messages"][1]["content"])
    material["turns"][0]["messages"][0]["content"] = "伪造的旧事实"
    row["request"]["messages"][1]["content"] = json.dumps(material, ensure_ascii=False)
    row["input_sha256"] = digest(row["request"])
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["input_hashes"]["1"] = row["input_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = report_suite(root)
    assert report["requests"][0]["state"] == "invalid" and report["usage_unknown"]


@pytest.mark.parametrize("fault", ["corpus", "manifest"])
def test_changed_corpus_or_schedule_cannot_produce_a_comparison(successful_run, fault):
    root, _ = successful_run
    path = root / ("cases.json" if fault == "corpus" else "manifest.json")
    data = json.loads(path.read_bytes())
    if fault == "corpus":
        data["revision"] += 1
    else:
        data["schedule"][0]["arm"] = "focused"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        report_suite(root)
