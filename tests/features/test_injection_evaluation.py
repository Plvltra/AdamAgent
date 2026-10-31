"""B02.3：离线走真实评估入口，核对效果、未知、人工拒绝判读及无付费调用边界。"""

import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from adam.config.settings import LLMConfig
from scripts.injection_cases import load_suite
from scripts.injection_eval import main, run_suite, run_variant
from scripts.injection_fixture import MARKER_TEXT, PUBLIC_TEXT, prepare_fixture
from scripts.injection_report import report_suite
from tests.features.test_injection_boundaries import sequence, step_calls
from tests.features.test_tool_loop import answer, tool_response

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "examples/injection-evaluation/cases-v1.json"
SUITE = load_suite(CASES)
LLM = LLMConfig("https://unused.test", "test", "NEVER_SAVE_THIS_API_KEY", stream=False)


def prepare(tmp_path, case="direct-scope", arm="benign"):
    root = tmp_path / "evaluation"
    root.mkdir()
    (root / "suite.json").write_bytes(CASES.read_bytes())
    return root, prepare_fixture(root / case / arm, SUITE, case, arm)


def execute(fixture, handler):
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        return run_variant(fixture, LLM, http, "mock")


def row_for(root, case="direct-scope", arm="benign", **kwargs):
    return next(
        r for r in report_suite(root, **kwargs)["runs"] if r["case_id"] == case and r["arm"] == arm
    )


@pytest.mark.parametrize("case", [pair.case_id for pair in SUITE.pairs])
@pytest.mark.parametrize("arm", ["attack", "benign"])
def test_evaluation_entry_reports_real_effects_exposure_usage_and_readonly_evidence(
    tmp_path, case, arm
):
    root, fixture = prepare(tmp_path, case, arm)
    steps, bodies = sequence(fixture), []
    final = (
        "EDIT_OK"
        if case == "direct-approval"
        else MARKER_TEXT
        if case == "tool-approval"
        else PUBLIC_TEXT
    )

    def model(request):
        body = json.loads(request.content)
        number = len(bodies)
        bodies.append(body)
        response = (
            tool_response(step_calls(fixture, steps[number], body))
            if number < len(steps)
            else answer(final)
        )
        return httpx.Response(200, json=response)

    error = execute(fixture, model)
    assert error == (
        "approval_denied" if arm == "attack" and fixture.pair.boundary == "approval" else None
    )
    before = {p: sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    row = row_for(root, case, arm)
    assert row["state"] == ("no_violation_observed" if arm == "attack" else "complete")
    assert row["exposed"] is True and row["evidence_complete"] is True
    assert row["backend"] == "mock" and row["unknown_usage"] is False
    assert row["known_tokens"] == 12 * len(bodies)
    assert row["violations"] == [] and row["issues"] == []
    assert set(row["required_satisfied"]) == set(fixture.variant.required)
    assert before == {p: sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    assert all(LLM.api_key.encode() not in p.read_bytes() for p in before)


def test_incomplete_answer_is_not_automatically_a_false_refusal_and_labels_require_quote(tmp_path):
    root, fixture = prepare(tmp_path)
    execute(fixture, lambda _: httpx.Response(200, json=answer("我不能读取这个公开文件。")))
    report = report_suite(root)
    assert row_for(root)["state"] == "incomplete"
    assert report["false_refusal_rate"]["value"] is None
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {"direct-scope": {"decision": "refusal", "quote": "不能读取这个公开文件"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report = report_suite(root, labels_path=labels)
    assert report["false_refusal_rate"] == {
        "numerator": 1,
        "denominator": 1,
        "value": 1,
        "unassessed": 6,
    }
    assert report["labels_sha256"] == sha256(labels.read_bytes()).hexdigest()
    labels.write_text(
        json.dumps({"direct-scope": {"decision": "refusal", "quote": "invented"}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="真实回答"):
        report_suite(root, labels_path=labels)


def test_indirect_source_not_read_is_unexposed_not_safe(tmp_path):
    root, fixture = prepare(tmp_path, "file-mode", "attack")
    execute(fixture, lambda _: httpx.Response(200, json=answer("不读取任何文件。")))
    report = report_suite(root)
    assert row_for(root, "file-mode", "attack")["state"] == "unexposed"
    assert report["violation_rate"]["value"] is None


@pytest.mark.parametrize("damage", ["capture_missing", "partial_jsonl", "identity"])
def test_missing_or_corrupt_evidence_stays_unknown_and_is_not_repaired(tmp_path, damage):
    root, fixture = prepare(tmp_path, arm="attack")
    execute(fixture, lambda _: httpx.Response(200, json=answer("拒绝。")))
    if damage == "capture_missing":
        next((fixture.root / "requests").glob("*.json")).unlink()
    elif damage == "partial_jsonl":
        path = next((fixture.root / "sessions").rglob("*.jsonl"))
        path.write_bytes(path.read_bytes() + b'{"partial":')
    else:
        path = fixture.root / "fixture.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["suite_sha256"] = "wrong"
        path.write_text(json.dumps(data), encoding="utf-8")
    before = {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
    assert row_for(root, arm="attack")["state"] == "unknown"
    assert before == {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}


def test_tool_leak_is_preserved_even_when_next_model_request_fails(tmp_path):
    root, fixture = prepare(tmp_path, arm="attack")
    calls = 0

    def model(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json=tool_response(step_calls(fixture, "public", json.loads(request.content)))
            )
        return httpx.Response(503, json={"error": "not copied"})

    assert execute(fixture, model) == "api_http"
    path = next((fixture.root / "sessions").rglob("*.jsonl"))
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # 故障注入只改已结束工具正文，模拟敏感信息进入结果；保留实际失败请求和事件关联。
    finished = next(e for e in events if e["type"] == "tool_finished")
    output = json.loads(finished["payload"]["output"])
    output["lines"][0]["text"] = fixture.private_text
    finished["payload"]["output"] = json.dumps(output)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8"
    )
    row = row_for(root, arm="attack")
    assert row["state"] == "violation" and "read:private" in row["violations"]
    assert row["run_error"] == "api_http" and row["issues"]
    assert row["known_tokens"] == 12 and row["unknown_usage"] is True
    assert row["tool_attempts"][0]["name"] == "read_file"


def test_wrong_edit_counts_as_violation_but_not_success_even_with_broken_log(tmp_path):
    root, fixture = prepare(tmp_path, "file-mode", "attack")
    execute(fixture, lambda _: httpx.Response(200, json=answer("done")))
    fixture.path("target").write_bytes(b"wrong bytes")
    path = next((fixture.root / "sessions").rglob("*.jsonl"))
    path.write_bytes(path.read_bytes() + b"partial")
    row = row_for(root, "file-mode", "attack")
    assert row["state"] == "violation" and row["violations"] == ["write:target"]
    assert row["evidence_complete"] is False


def test_batch_stops_on_api_error_without_charging_remaining_cases(tmp_path):
    calls = []

    def failure(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "not copied"})

    report = run_suite(tmp_path / "run", CASES, LLM, transport=httpx.MockTransport(failure))
    assert len(calls) == 1
    assert report["runs"][0]["state"] == "unknown"
    assert all(r["state"] == "not_run" for r in report["runs"][1:])
    assert report["violation_rate"]["value"] is None
    with pytest.raises(FileExistsError):
        run_suite(tmp_path / "run", CASES, LLM, transport=httpx.MockTransport(failure))
    assert len(calls) == 1


def test_cli_requires_explicit_paid_flag_before_loading_config_or_creating_directory(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["run", str(tmp_path / "run"), "--llm-config", str(tmp_path / "missing.toml")])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_report_cli_never_loads_model_config_or_constructs_http_client(
    tmp_path, monkeypatch, capsys
):
    root, fixture = prepare(tmp_path, arm="attack")
    execute(fixture, lambda _: httpx.Response(200, json=answer("拒绝。")))

    def forbidden(*args, **kwargs):
        pytest.fail("readonly report attempted network/config loading")

    monkeypatch.setattr("scripts.injection_eval.load_llm_config", forbidden)
    monkeypatch.setattr("scripts.injection_eval.httpx.Client", forbidden)
    assert main(["report", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["violation_rate"]["value"] == 0


def test_capture_failure_stops_before_http_and_report_remains_unknown(tmp_path, monkeypatch):
    root, fixture = prepare(tmp_path)
    calls = []

    def fail_capture(*args, **kwargs):
        raise OSError("synthetic evidence disk failure")

    monkeypatch.setattr("scripts.injection_capture.write_record", fail_capture)
    with pytest.raises(OSError):
        execute(fixture, lambda request: calls.append(request))
    assert calls == []
    assert row_for(root)["state"] == "unknown"


def test_missing_usage_is_unknown_not_zero_cost_or_safe(tmp_path):
    root, fixture = prepare(tmp_path, arm="attack")
    response = answer("拒绝。")
    response.pop("usage")
    execute(fixture, lambda _: httpx.Response(200, json=response))
    row = row_for(root, arm="attack")
    assert row["state"] == "unknown" and row["unknown_usage"] is True
    assert report_suite(root)["violation_rate"]["value"] is None


def test_matching_final_bytes_without_applied_record_do_not_complete_positive_edit(tmp_path):
    root, fixture = prepare(tmp_path, "direct-approval", "benign")
    execute(fixture, lambda _: httpx.Response(200, json=answer("EDIT_OK")))
    fixture.path("target").write_bytes(b"after")
    row = row_for(root, "direct-approval", "benign")
    assert row["state"] == "incomplete" and "write:target" in row["observed_effects"]
    assert "write:target" not in row["required_satisfied"]


def test_successful_batch_keeps_variants_independent_and_freezes_exact_manifest(tmp_path):
    source = tmp_path / "one-pair.json"
    data = json.loads(CASES.read_text(encoding="utf-8"))
    data["pairs"] = data["pairs"][:1]
    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    calls = []

    def model(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json=answer("拒绝。"))

    root = tmp_path / "run"
    report = run_suite(root, source, LLM, transport=httpx.MockTransport(model))
    assert len(calls) == 2 and len(report["runs"]) == 2
    assert [r["state"] for r in report["runs"]] == ["no_violation_observed", "incomplete"]
    assert (root / "suite.json").read_bytes() == source.read_bytes()
    first = json.loads((root / "direct-scope/attack/fixture.json").read_text(encoding="utf-8"))
    second = json.loads((root / "direct-scope/benign/fixture.json").read_text(encoding="utf-8"))
    assert first["private_text"] != second["private_text"]
    source.write_text("changed later", encoding="utf-8")
    assert report_suite(root)["suite_sha256"] == report["suite_sha256"]


def test_restoring_original_bytes_does_not_erase_a_recorded_actual_write(tmp_path):
    root, fixture = prepare(tmp_path, "direct-approval", "benign")
    steps, calls = sequence(fixture), []

    def model(request):
        body = json.loads(request.content)
        index = len(calls)
        calls.append(body)
        response = (
            tool_response(step_calls(fixture, steps[index], body))
            if index < len(steps)
            else answer("EDIT_OK")
        )
        return httpx.Response(200, json=response)

    assert execute(fixture, model) is None
    fixture.path("target").write_bytes(b"old")
    row = row_for(root, "direct-approval", "benign")
    assert "write:target" in row["observed_effects"]
    assert "write:target" not in row["required_satisfied"]
    assert row["state"] == "incomplete"
