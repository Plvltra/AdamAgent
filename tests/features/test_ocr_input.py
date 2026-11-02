"""C04.3：真实 OCR 经普通聊天工具保存文字与原页证据，远端模型使用模拟传输。"""

import hashlib
import importlib.util
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from adam.config.settings import VisionConfig
from adam.contracts.media import decode_media_reference
from tests.features.test_image_input import image_app, image_config, media_events
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def ocr_config(root, models=None):
    spec = importlib.util.find_spec("rapidocr")
    assert spec is not None and spec.origin is not None, "Install the ocr extra for OCR tests"
    model_path = models or Path(spec.origin).parent / "models"
    return replace(
        image_config(root), vision=VisionConfig(enabled=True, ocr_models=str(model_path))
    )


def recognize_then_answer(body, number):
    return (
        tool_response([call("ocr", "read_ocr", path="input", page_count=2)])
        if number == 1
        else answer("recognized")
    )


@pytest.mark.parametrize("filename", ["invoice.png", "mixed.pdf"])
def test_real_ocr_persists_original_evidence_and_coordinates(tmp_path, filename):
    source = Path(__file__).parents[1] / "fixtures" / "ocr" / filename
    target = tmp_path / filename
    shutil.copyfile(source, target)

    def respond(body, number):
        return (
            tool_response([call("ocr", "read_ocr", path=filename, page_count=2)])
            if number == 1
            else answer("recognized")
        )

    with image_app(tmp_path, respond, config=ocr_config(tmp_path)) as (app, store, media, bodies):
        app.send("识别原文")
        result = outputs(bodies[-1])[-1]
        assert result["ok"], result
        if filename.endswith("pdf"):
            assert result["version"] == hashlib.sha256(source.read_bytes()).hexdigest()
            first, second = result["pages"]
            assert "Original digital text" in first["text_layer"]["text"]
            assert first["ocr"] is None and first["image"] is None
            assert second["page"] == 2
            assert second["coordinate_space"] == "rendered_page_pixels"
            result = second
        recognized = result["ocr"]
        assert "INVOICE" in " ".join(line["text"] for line in recognized["lines"])
        for line in recognized["lines"]:
            assert len(line["polygon"]) == 4
            assert line["score"] is None or 0 <= line["score"] <= 1
        ref = decode_media_reference(result["image"])
        assert ref.version == recognized["version"]
        assert ref.source.tool_name == "read_ocr" and ref.source.tool_call_id == "ocr"
        saved = media.read(ref.media_id, expected_version=ref.version)
        assert hashlib.sha256(saved.data).hexdigest() == recognized["version"]
        assert len(media_events(store, app.session.session_id)) == 1


def test_missing_models_returns_tool_failure_without_media(tmp_path):
    shutil.copyfile(Path(__file__).parents[1] / "fixtures/ocr/invoice.png", tmp_path / "input")
    with image_app(
        tmp_path, recognize_then_answer, config=ocr_config(tmp_path, tmp_path / "absent")
    ) as (app, store, _, bodies):
        app.send("识别")
        result = outputs(bodies[-1])[-1]
        assert result["ok"] is False and result["status"] == "model_missing"
        assert media_events(store, app.session.session_id) == []
