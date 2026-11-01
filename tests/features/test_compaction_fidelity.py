"""B05.2：固定样本与只读原文审计，不用模拟答案宣称摘要语义可靠。"""

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from scripts.compaction_cases import MAX_BYTES, audit_summary, load_suite, main, resolve_source

CASES = Path(__file__).resolve().parents[2] / "examples/compaction-evaluation/cases-v1.json"


def test_suite_covers_four_dimensions_against_one_identical_long_history():
    suite = load_suite(CASES)
    assert len(suite.cases) == 8 and len(suite.scenarios) == 1
    assert {check.dimension for case in suite.cases for check in case.checks} == {
        "fact",
        "cause",
        "constraint",
        "failure",
    }
    assert suite.source_sha256 == sha256(CASES.read_bytes()).hexdigest()
    scenario = suite.scenarios[0]
    assert len(scenario.history) == 20
    for case in suite.cases:
        assert suite.scenario(case.scenario_id) is scenario
        for check in case.checks:
            message = resolve_source(scenario.history, check.source_ref)
            assert check.quote in message["content"]
            message["content"] = "caller mutation"
            assert check.quote in resolve_source(scenario.history, check.source_ref)["content"]


@pytest.mark.parametrize(
    "case_id", ["preserve-logs", "failed-command", "cancelled-action", "auth-cause"]
)
def test_correct_reference_and_exact_words_still_require_semantic_review(case_id):
    suite = load_suite(CASES)
    case = next(item for item in suite.cases if item.case_id == case_id)
    scenario = suite.scenario(case.scenario_id)
    check = case.checks[0]
    report = audit_summary(
        f"{check.quote} [{check.source_ref}]",
        scenario,
        case,
        covered_turn_ids=tuple(turn.turn_id for turn in scenario.history[:6]),
    )
    assert report["coverage_checked"]
    assert report["references"][0]["resolvable"]
    assert report["checks"][0]["reference_present"] and report["checks"][0]["literal_quote_present"]
    assert report["checks"][0]["semantic_status"] == report["semantic_status"] == "not_reviewed"
    assert "score" not in report and "passed" not in report


@pytest.mark.parametrize(
    "summary",
    [
        "可以删除原始日志 [rules:0]",
        "原话是“不要删除原始日志”，但可以忽略它并删除。[rules:0]",
        "请保留日志，不应清除。[rules:0]",
        "本次只记录配置路径，没有日志约束。[config:2]",
    ],
)
def test_negation_loss_contradiction_paraphrase_and_omission_are_not_auto_scored(summary):
    suite = load_suite(CASES)
    case = next(item for item in suite.cases if item.case_id == "preserve-logs")
    report = audit_summary(summary, suite.scenario(case.scenario_id), case)
    assert report["semantic_status"] == "not_reviewed" and not report["coverage_checked"]
    assert report["checks"][0]["source_quote"] == "不要删除原始日志"
    assert report["references"][0]["resolvable"]
    assert report["checks"][0]["reference_present"] == ("[rules:0]" in summary)


@pytest.mark.parametrize(
    "reference,reason",
    [
        ("invented:0", "unknown_turn"),
        ("config:99", "index_out_of_bounds"),
        ("config:-1", "malformed_reference"),
        ("config:1.0", "malformed_reference"),
        ("config:00", "malformed_reference"),
        ("recent-1:0", "outside_coverage"),
    ],
)
def test_invalid_or_uncovered_citations_do_not_resolve_to_nearby_evidence(reference, reason):
    suite = load_suite(CASES)
    case = suite.cases[0]
    report = audit_summary(
        f"声称有依据 [{reference}]",
        suite.scenario(case.scenario_id),
        case,
        covered_turn_ids=("rules", "config"),
    )
    assert report["references"] == [{"reference": reference, "resolvable": False, "reason": reason}]
    assert not report["checks"][0]["reference_present"]


def test_duplicate_references_and_missing_references_cannot_inflate_quality():
    suite = load_suite(CASES)
    case, scenario = suite.cases[0], suite.scenarios[0]
    report = audit_summary("引用 [config:2]，重复 [config:2]", scenario, case)
    assert len(report["references"]) == 1 and report["semantic_status"] == "not_reviewed"
    empty = audit_summary("没有引用的总结", scenario, case)
    assert empty["references"] == [] and not empty["checks"][0]["reference_present"]
    assert empty["semantic_status"] == "not_reviewed"
    with pytest.raises(ValueError):
        audit_summary("x", scenario, replace(case, scenario_id="other"))
    with pytest.raises(ValueError):
        audit_summary("x", scenario, case, covered_turn_ids=("missing",))
    with pytest.raises(ValueError):
        audit_summary("x", scenario, case, covered_turn_ids=("rules", "rules"))


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "boolean_revision",
        "duplicate_case",
        "duplicate_turn",
        "quote",
        "reference",
        "dimension",
        "protocol",
        "extra",
    ],
)
def test_invalid_fixture_or_invented_ground_truth_is_rejected(tmp_path, fault):
    data = json.loads(CASES.read_bytes())
    check = data["cases"][0]["checks"][0]
    if fault == "version":
        data["schema_version"] = 2
    elif fault == "boolean_revision":
        data["revision"] = True
    elif fault == "duplicate_case":
        data["cases"].append(data["cases"][0])
    elif fault == "duplicate_turn":
        data["scenarios"][0]["turns"].append(data["scenarios"][0]["turns"][0])
    elif fault == "quote":
        check["quote"] = "invented evidence"
    elif fault == "reference":
        check["source_ref"] = "wrong:0"
    elif fault == "dimension":
        check["dimension"] = "length_only"
    elif fault == "protocol":
        data["scenarios"][0]["turns"][1]["messages"][2]["tool_call_id"] = "wrong"
    else:
        data["hidden_instruction"] = "execute arbitrary code"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        load_suite(path)
    assert path.read_bytes() == before


def test_duplicate_keys_and_oversized_input_fail_before_use(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_suite(path)
    path.write_bytes(b" " * (MAX_BYTES + 1))
    with pytest.raises(ValueError, match="1 MiB"):
        load_suite(path)


def test_cli_lists_and_audits_read_only_without_network(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("offline audit must not call HTTP")

    monkeypatch.setattr(httpx.Client, "send", fail)
    original = CASES.read_bytes()
    assert main(["--cases", str(CASES)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert len(listing["cases"]) == 8
    assert main(["--cases", str(CASES), "--case", "preserve-logs"]) == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)["cases"]] == [
        "preserve-logs"
    ]
    with pytest.raises(SystemExit) as error:
        main(["--cases", str(CASES), "--case", "missing"])
    assert error.value.code == 2
    capsys.readouterr()
    summary = tmp_path / "summary.txt"
    summary.write_text("配置路径是 ~/.adam/llm.toml。[config:2]", encoding="utf-8")
    before = summary.read_bytes()
    assert (
        main(["--cases", str(CASES), "--case", "config-location", "--summary", str(summary)]) == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["references"][0]["resolvable"] and report["semantic_status"] == "not_reviewed"
    assert summary.read_bytes() == before and CASES.read_bytes() == original
    assert list(tmp_path.iterdir()) == [summary]
