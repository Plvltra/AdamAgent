"""B17.3.2 用真实 PDF 走完整本地验收；原文件保留，报告不复制原文，不调用模型。"""

import hashlib
import json
import sys

import pytest

from adam.config.loader import load_config
from adam.contracts.errors import ConfigError
from adam.documents.index import DocumentIndex
from adam.documents.models import DocumentError
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.policy import PathPolicy
from scripts.pdf_acceptance import main, verify
from tests.features.support.pdf_data import make_pdf
from tests.test_config import LLM


def test_complete_pdf_acceptance_covers_mixed_pages_and_changes_only_temporary_copy(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("local PDF acceptance invoked network or an external process")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    source = tmp_path / "source.pdf"
    data = make_pdf(
        [{"text": "Cover"}, {"image": True}, {"text": "retry twice", "image": True}, {}]
    )
    source.write_bytes(data)
    result = verify(source, "retry")
    assert source.read_bytes() == data
    assert result["source_version"] == hashlib.sha256(data).hexdigest()
    assert result["physical_pages"] == result["observed_pages"] == 4
    assert result["retrieved_pages"] == [3]
    assert result["notices"] == {"image_only": 1, "unread_images": 1, "no_extractable_text": 1}
    assert result["read_matches_first_fragment"] and result["old_version_rejected"]
    assert result["copy_generation_changed"] and result["source_metadata_unchanged"]
    assert result["paid_model_requests"] == 0 and result["visual_analysis"] == "not_performed"
    assert "retry twice" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


def test_cli_saves_metadata_report_exclusively_and_does_not_overwrite(tmp_path, monkeypatch):
    source, output = tmp_path / "source.pdf", tmp_path / "report.json"
    source.write_bytes(make_pdf([{"text": "retry twice"}]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["pdf_acceptance", "--pdf", str(source), "--query", "retry", "--output", str(output)],
    )
    assert main() == 0
    saved = output.read_bytes()
    assert json.loads(saved)["real_pdf_parser"]
    with pytest.raises(DocumentError, match="已经存在"):
        main()
    assert output.read_bytes() == saved


@pytest.mark.parametrize("failure", ["encrypted", "no_match"])
def test_real_failure_does_not_create_a_pass_report(tmp_path, monkeypatch, failure):
    source, output = tmp_path / "source.pdf", tmp_path / "report.json"
    source.write_bytes(
        make_pdf([{"text": "other text"}], password="secret" if failure == "encrypted" else None)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["pdf_acceptance", "--pdf", str(source), "--query", "missing", "--output", str(output)],
    )
    with pytest.raises(DocumentError):
        main()
    assert not output.exists()


def test_explicit_toml_stream_limit_applies_to_reading_and_full_index(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "large-stream.pdf"
    source.write_bytes(make_pdf([{"text": "retry " * 400}]))
    main = tmp_path / "config.toml"
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")

    def configured(size):
        main.write_text(
            f'[documents]\nroots=["docs"]\nmax_pdf_stream_bytes={size}\n'
            f"[tool_limits]\nmax_pdf_stream_bytes={size}\n"
        )
        return load_config(main)

    for size, permitted in ((512, False), (8192, True)):
        config = configured(size)
        registry = create_tool_registry(tmp_path, limits=config.tool_limits)
        result = json.loads(registry.execute("read_pdf", json.dumps({"path": str(source)})))
        assert result["ok"] is permitted
        index = DocumentIndex(
            tmp_path / "cache",
            tmp_path,
            config.documents,
            PathPolicy(tmp_path, config.permissions).resolve,
        )
        if permitted:
            assert result["pages"][0]["text"].startswith("retry")
            assert index.refresh().chunks
        else:
            assert "超限" in result["error"]
            with pytest.raises(DocumentError, match="超限"):
                index.refresh()
    for invalid in (0, 8388609, "true"):
        with pytest.raises(ConfigError):
            configured(invalid)
