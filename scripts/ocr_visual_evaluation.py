"""C04.4 固定样例真实 OCR/视觉对照；原始失败保留，不重试或按结果调题。"""

import argparse
import hashlib
import importlib.metadata
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.bootstrap import open_image_questions
from adam.config.settings import AgentConfig, ContextConfig
from adam.config.vision import LocalVisionConfig
from adam.contracts.media import decode_media_reference
from adam.session.storage import SessionStore
from scripts.vision_acceptance import write_json
from tests.features.test_image_input import image_app, load_then_answer
from tests.features.test_ocr_input import ocr_config
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


def edit_distance(expected: str, actual: str) -> int:
    """逐 Unicode 字符计算编辑距离；保留空格和标点，避免掩盖转写错误。"""
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def overlap(expected: list[float], polygon: list[list[float]]) -> float:
    """返回预期文本矩形与识别四点外接矩形的 IoU；旋转样例也用原像素坐标。"""
    x0, y0 = min(p[0] for p in polygon), min(p[1] for p in polygon)
    x1, y1 = max(p[0] for p in polygon), max(p[1] for p in polygon)
    a, b, c, d = expected
    intersection = max(0, min(x1, c) - max(x0, a)) * max(0, min(y1, d) - max(y0, b))
    union = (x1 - x0) * (y1 - y0) + (c - a) * (d - b) - intersection
    return intersection / union if union > 0 else 0.0


def score_ocr(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any]:
    """按框最大重叠一对一匹配，漏检/多检计入字符错误；定位另报 IoU>=0.5 比例。

    匹配不使用识别文字，避免把文字正确性混入定位评分。匹配阈值与计分规则在推理前固定。
    """
    remaining = set(range(len(actual)))
    matches = []
    errors = 0
    for line in expected:
        chosen = max(
            remaining, key=lambda i: overlap(line["box"], actual[i]["polygon"]), default=None
        )
        iou = overlap(line["box"], actual[chosen]["polygon"]) if chosen is not None else 0.0
        if iou == 0:
            chosen = None
        text = actual[chosen]["text"] if chosen is not None else ""
        if chosen is not None:
            remaining.remove(chosen)
        distance = edit_distance(line["text"], text)
        errors += distance
        matches.append(
            {"expected": line["text"], "actual": text, "iou": iou, "edit_distance": distance}
        )
    errors += sum(len(actual[i]["text"]) for i in remaining)
    count = sum(len(line["text"]) for line in expected)
    return {
        "matches": matches,
        "extra_lines": [actual[i] for i in sorted(remaining)],
        "character_errors": errors,
        "reference_characters": count,
        "cer": errors / count if count else None,
        "located_lines": sum(m["iou"] >= 0.5 for m in matches),
        "expected_lines": len(expected),
    }


def run(manifest: Path, root: Path, local: LocalVisionConfig) -> None:
    """校验固定输入哈希后逐样例运行正式工具与问答入口；临时会话中留原始请求用量。"""
    fixed = json.loads(manifest.read_text(encoding="utf-8-sig"))
    fixtures = manifest.parent
    for case in fixed["cases"]:
        for key, digest in (("path", "sha256"), ("vision_image", "vision_sha256")):
            assert hashlib.sha256((fixtures / case[key]).read_bytes()).hexdigest() == case[digest]
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "manifest.json", fixed)
    write_json(
        root / "runtime.json",
        {
            "local": asdict(local),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("rapidocr", "onnxruntime", "pypdfium2", "pillow")
            },
            "cost": None,
            "currency": None,
            "mock_scope": "tool selection/loading only; OCR and vision are real",
        },
    )
    results = []
    for case in fixed["cases"]:
        work = root / case["id"]
        work.mkdir()
        filename = Path(case["path"]).name
        shutil.copyfile(fixtures / case["path"], work / filename)

        def respond(body: dict[str, Any], number: int, filename: str = filename) -> dict[str, Any]:
            return (
                tool_response([call("ocr", "read_ocr", path=filename, page_count=2)])
                if number == 1
                else answer("OCR recorded")
            )

        item: dict[str, Any] = {
            "id": case["id"],
            "kind": case["kind"],
            "question": case["question"],
            "expected_answer": case["expected_answer"],
            "input_sha256": case["sha256"],
        }
        with image_app(work, respond, config=ocr_config(work)) as (app, store, _, bodies):
            app.send("Read OCR only")
            output = outputs(bodies[-1])[-1]
            item["ocr_result"] = output
            assert app.session is not None
            write_json(work / "ocr_events.json", store.load_events(app.session.session_id))
        if output.get("ok"):
            recognized = output["pages"][1]["ocr"] if filename.endswith(".pdf") else output["ocr"]
            item["ocr_score"] = score_ocr(case["lines"], recognized["lines"])
        visual = work / "vision"
        visual.mkdir()
        shutil.copyfile(fixtures / case["vision_image"], visual / "sample.bin")
        with image_app(visual, load_then_answer) as (app, _, _, bodies):
            app.send("Load original image only")
            reference = decode_media_reference(outputs(bodies[-1])[-1]["media"])
            assert app.session is not None
            sid = app.session.session_id
        with open_image_questions(
            visual / "sessions",
            sid,
            local,
            limits=AgentConfig(
                max_model_requests=1, max_session_tokens=50000, max_output_tokens=512
            ),
            context=ContextConfig(
                max_input_tokens=16000, safety_margin_tokens=1024, auto_compact=False
            ),
        ) as qa:
            try:
                item["vision_answer"] = asdict(qa.ask(case["question"], reference))
            except Exception as error:
                item["vision_error"] = {"type": type(error).__name__, "message": str(error)}
        with SessionStore(visual / "sessions") as store:
            item["vision_events"] = store.load_events(sid)
        results.append(item)
        write_json(root / "results.json", results)
        print(case["id"] + " saved", flush=True)
        if "vision_error" in item:
            raise RuntimeError("视觉请求失败，结果已保存；停止批次，不自动重试。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.manifest, args.output, LocalVisionConfig(model="adam-vision", timeout_seconds=180))
