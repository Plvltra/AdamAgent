"""固定语义目录对照须保留实际命中、降级和权限证据，不把替身或字节当真实效果。"""

import json

import pytest

from adam.contracts.execution import ExecutionMode
from adam.contracts.tool_catalog import tool_schema_record
from scripts import tool_discovery_eval as evaluation


class EqualVectors:
    """故意使候选同分，测报告是否如实记录误召回；不模拟真正理解查询。"""

    identity = "test-equal-vectors-v1"

    def __init__(self):
        self.batches = []

    def passages(self, texts):
        self.batches.append(texts)
        return [[1.0, 0.0] for _ in texts]

    def query(self, text):
        return [1.0, 0.0]


def test_fixed_twenty_tool_report_preserves_candidates_costs_and_preencoding_filter(tmp_path):
    backend = EqualVectors()
    directory = tmp_path / "run"
    report = evaluation.evaluate(directory, tmp_path / "unused", backend=backend)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["catalog"]) == 20 and len(manifest["cases"]) == 12
    assert report == json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert report["backend"] == "injected" and len(report["rows"]) == 24
    groups = report["groups"]
    assert groups["keyword"]["all_expected_cases"] == 1
    assert groups["keyword"]["recall_at_3"] == 0.1
    assert groups["keyword"]["encoding"]["query_calls"] == 0
    assert groups["semantic"]["fallback_cases"] == 0
    assert groups["semantic"]["extra_candidates"] > 0
    assert groups["semantic"]["correct_empty_cases"] == 0
    assert groups["semantic"]["encoding"]["passage_batches"] == 2
    assert groups["semantic"]["encoding"]["passage_texts"] == 35
    assert groups["semantic"]["encoding"]["query_calls"] == 12
    assert groups["semantic"]["forbidden_selected"] == 0
    assert len(backend.batches) == 2 and len(backend.batches[1]) == 15
    assert not any(
        text.startswith(("replace_text ", "run_command ")) for text in backend.batches[1]
    )
    exact = next(row for row in report["rows"] if row["method"] == "semantic")
    assert exact["selected"][0] == "git_log" and exact["first_semantic_query"]
    assert exact["page"]["next_offset"] == 3
    assert exact["page"]["ranking"]["directory_version"] == manifest["directory_version"]
    assert exact["page"]["ranking"]["embedding"] == backend.identity
    assert len(exact["selected_schema"]["names"]) == 5
    assert len(exact["initial_schema"]["names"]) == 2
    assert exact["all_allowed_schema"] == tool_schema_record(manifest["schemas"])
    for row in report["rows"]:
        assert row["elapsed_ms"] >= 0 and row["candidate_result_bytes"] > 0
        assert not row["forbidden_selected"]
        assert row["matched"] == sorted(set(row["selected"]) & set(row["expected"]))
    assert len(list(directory.glob("*.json"))) == 26


@pytest.mark.parametrize("fault", ["missing", "invalid"])
def test_model_failure_is_counted_as_fallback_without_secret_or_false_semantic_success(
    tmp_path, fault
):
    class Broken(EqualVectors):
        def passages(self, texts):
            if fault == "missing":
                raise RuntimeError("PRIVATE_MODEL_ERROR")
            return [[float("nan")] for _ in texts]

    report = evaluation.evaluate(tmp_path / "run", tmp_path / "unused", backend=Broken())
    group = report["groups"]["semantic"]
    assert group["fallback_cases"] == 12 and group["all_expected_cases"] == 1
    assert group["correct_empty_cases"] == 2 and group["forbidden_selected"] == 0
    assert "PRIVATE_MODEL_ERROR" not in json.dumps(report)
    assert all(
        row["page"]["ranking"]["embedding"] is None
        for row in report["rows"]
        if row["method"] == "semantic"
    )


def test_default_backend_uses_explicit_cache_without_download_and_cli_marks_local_model(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def factory(cache, model, *, allow_download=False):
        calls.append((cache, model, allow_download))
        return EqualVectors()

    monkeypatch.setattr(evaluation, "FastEmbedBackend", factory)
    cache = tmp_path / "prepared"
    assert evaluation.main([str(tmp_path / "run"), "--model-cache", str(cache)]) == 0
    assert calls == [(cache / "models", "BAAI/bge-small-zh-v1.5", False)]
    assert json.loads(capsys.readouterr().out)["backend"] == "local_model"


def test_existing_evidence_is_not_overwritten_and_cli_does_not_print_paths(tmp_path, capsys):
    evidence = tmp_path / "PRIVATE_DIRECTORY"
    evidence.mkdir()
    record = evidence / "report.json"
    record.write_text("original", encoding="utf-8")
    assert evaluation.main([str(evidence), "--model-cache", str(tmp_path)]) == 1
    assert "PRIVATE_DIRECTORY" not in capsys.readouterr().err
    assert record.read_text(encoding="utf-8") == "original"


def test_interrupted_report_keeps_completed_rows_without_final_success(tmp_path, monkeypatch):
    original = evaluation.write_record

    def fail(path, *args, **kwargs):
        if path.name == "03-keyword.json":
            raise OSError("disk full")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(evaluation, "write_record", fail)
    directory = tmp_path / "run"
    with pytest.raises(OSError):
        evaluation.evaluate(directory, tmp_path, backend=EqualVectors())
    assert (directory / "manifest.json").is_file()
    assert (directory / "02-keyword.json").is_file()
    assert not (directory / "report.json").exists()


def test_plan_directory_excludes_side_effects_even_when_descriptions_are_similar():
    directory = evaluation.catalog_registry().directory()
    plan = {entry.name for entry in directory.listings(ExecutionMode.PLAN)}
    assert len(plan) == 15
    assert not plan & {"create_file", "replace_text", "apply_patch", "delete_file", "run_command"}
