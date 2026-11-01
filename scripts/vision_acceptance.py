"""固定本地视觉冒烟验收；测试 fixture 只编排真实图片加载，不替代视觉回答。"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageFont

from adam.bootstrap import open_image_questions
from adam.config.settings import AgentConfig, ContextConfig
from adam.config.vision import LocalVisionConfig
from adam.contracts.media import decode_media_reference
from adam.session.storage import SessionStore
from tests.features.test_image_input import image_app, load_then_answer
from tests.features.test_subagents import outputs

FONT = Path("C:/Windows/Fonts/arialbd.ttf")
COLORS = {"red": "#dc2938", "blue": "#2166cd", "green": "#2b9943", "orange": "#ec871b"}
CASES = [
    {
        "id": "labels_words",
        "image": "labels.png",
        "question": "What exact word is printed above the red object, and what exact word is printed above the blue object?",
        "expected": {"word_above_red": "ORBIT", "word_above_blue": "HARBOR"},
    },
    {
        "id": "labels_geometry",
        "image": "labels.png",
        "question": "Compare the red and blue objects: identify each shape, say which is farther left, and say whether the text labels are above or below their corresponding objects.",
        "expected": {
            "red_shape": "circle",
            "blue_shape": "square",
            "farther_left": "red circle",
            "text_relative_to_shapes": "above",
        },
    },
    {
        "id": "layout_a_relations",
        "image": "layout_a.png",
        "question": "Which colored shapes are inside the orange outline, which one is higher than the other, and is the red square to the left or right of the outlined region?",
        "expected": {
            "inside": ["blue circle", "green triangle"],
            "higher": "blue circle",
            "lower": "green triangle",
            "red_square_relative_to_outline": "right",
            "red_square_inside": False,
        },
    },
    {
        "id": "layout_b_relations",
        "image": "layout_b.png",
        "question": "Which colored shapes are inside the orange outline, which one is higher than the other, and is the red square to the left or right of the outlined region?",
        "expected": {
            "inside": ["blue circle", "green triangle"],
            "higher": "green triangle",
            "lower": "blue circle",
            "red_square_relative_to_outline": "left",
            "red_square_inside": False,
        },
    },
]


def write_json(path: Path, value: object) -> None:
    """保存完整验收证据，不覆盖项目源码或真实用户会话。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate(root: Path) -> None:
    """生成固定像素样本和事先确定的答案；不因模型输出改题或调图。"""
    root.mkdir(parents=True, exist_ok=False)
    images = root / "images"
    images.mkdir(exist_ok=True)
    font = ImageFont.truetype(str(FONT), 48)
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((205, 125), "ORBIT", fill="#111111", font=font, anchor="mm")
    draw.text((595, 125), "HARBOR", fill="#111111", font=font, anchor="mm")
    draw.ellipse((110, 230, 300, 420), fill=COLORS["red"])
    draw.rectangle((500, 230, 690, 420), fill=COLORS["blue"])
    image.save(images / "labels.png", optimize=False)
    for name, mirrored in (("layout_a.png", False), ("layout_b.png", True)):
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        cx = 585 if mirrored else 215
        outline = (445, 60, 725, 540) if mirrored else (75, 60, 355, 540)
        draw.rectangle(outline, outline=COLORS["orange"], width=12)
        circle_y, triangle_y = (410, 180) if mirrored else (180, 410)
        draw.ellipse((cx - 62, circle_y - 62, cx + 62, circle_y + 62), fill=COLORS["blue"])
        draw.polygon(
            ((cx, triangle_y - 70), (cx - 76, triangle_y + 62), (cx + 76, triangle_y + 62)),
            fill=COLORS["green"],
        )
        square_x = 205 if mirrored else 595
        draw.rectangle((square_x - 70, 230, square_x + 70, 370), fill=COLORS["red"])
        image.save(images / name, optimize=False)
    write_json(
        root / "fixed_cases.json",
        {
            "created_utc": datetime.now(UTC).isoformat(),
            "sample_type": "Pillow synthetic raster, not screenshots; no user images",
            "font_path": str(FONT),
            "font_sha256": hashlib.sha256(FONT.read_bytes()).hexdigest(),
            "image_size": [800, 600],
            "image_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in images.glob("*.png")
            },
            "cases": CASES,
            "scoring": "Human semantic review of all expected facts; any incorrect or missing required fact fails the case. Paired layouts must produce opposite vertical and horizontal relations. No retries or question retuning.",
        },
    )
    print("Generated fixed images and questions.", flush=True)


def run(
    root: Path,
    local: LocalVisionConfig,
    *,
    manifest: Path | None = None,
    process_record: Path | None = None,
) -> None:
    """先经真实 load_image 保存来源，释放应用锁后以正式 SSE 工厂逐题问图。"""
    fixed = json.loads((root / "fixed_cases.json").read_text(encoding="utf-8"))
    assert fixed["cases"] == CASES
    for name, digest in fixed["image_sha256"].items():
        assert hashlib.sha256((root / "images" / name).read_bytes()).hexdigest() == digest
    limits = AgentConfig(max_model_requests=1, max_session_tokens=50000, max_output_tokens=512)
    context = ContextConfig(max_input_tokens=16000, safety_margin_tokens=1024, auto_compact=False)
    metadata: dict[str, Any] = {
        "started_utc": datetime.now(UTC).isoformat(),
        "artifacts_manifest": json.loads(manifest.read_text(encoding="utf-8-sig"))
        if manifest
        else None,
        "server_process": json.loads(process_record.read_text(encoding="utf-8-sig"))
        if process_record
        else None,
        "python": sys.version,
        "platform": platform.platform(),
        "packages": {p: importlib.metadata.version(p) for p in ("pillow", "httpx", "pytest")},
        "local_config": asdict(local),
        "limits": asdict(limits),
        "context": asdict(context),
        "official_entry": "adam.bootstrap.open_image_questions / ImageQuestions.ask",
        "load_entry": "tests.features.test_image_input.image_app + load_then_answer -> real load_image",
        "load_only_http": "MockTransport (not a visual answer; isolated per image)",
        "vision_http": "real 127.0.0.1 llama.cpp, SSE, no remote provider, no retries",
        "price": {"cost": None, "currency": None, "cost_status": "unpriced"},
    }
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as http:
        models = http.get(local.endpoint + "/models")
        metadata["models_http_status"] = models.status_code
        metadata["models"] = models.json()
    write_json(root / "runtime.json", metadata)
    results: list[dict[str, Any]] = []
    for image_name in ("labels.png", "layout_a.png", "layout_b.png"):
        work = root / "runs" / Path(image_name).stem
        work.mkdir(parents=True, exist_ok=False)
        (work / "sample.bin").write_bytes((root / "images" / image_name).read_bytes())
        with image_app(work, load_then_answer) as (app, store, _media, bodies):
            load_reply = app.send(
                "Load the local sample only; do not infer or describe image contents."
            )
            assert load_reply == "loaded, not viewed"
            reference = decode_media_reference(outputs(bodies[-1])[-1]["media"])
            sid = app.session.session_id
            load_evidence = {
                "reply": load_reply,
                "image_reference": asdict(reference),
                "mock_loading_request_count": len(bodies),
                "events": store.load_events(sid),
            }
            write_json(work / "load_evidence.json", load_evidence)
        # 原应用退出后才由问答工厂重新取得同一临时会话的写锁。
        with open_image_questions(
            work / "sessions", sid, local, limits=limits, context=context
        ) as qa:
            for case in (c for c in CASES if c["image"] == image_name):
                started = time.perf_counter_ns()
                item: dict[str, Any] = {**case, "image_path": str(root / "images" / image_name)}
                try:
                    result = qa.ask(case["question"], reference)
                    item["answer"] = asdict(result)
                    item["transport_success"] = True
                    print(case["id"] + ": " + result.text, flush=True)
                except Exception as error:
                    item["transport_success"] = False
                    item["error"] = {"type": type(error).__name__, "message": str(error)}
                    print(case["id"] + ": " + repr(error), flush=True)
                item["wall_duration_ms"] = (time.perf_counter_ns() - started) // 1_000_000
                item["image_sha256"] = fixed["image_sha256"][image_name]
                item["cost"] = None
                item["currency"] = None
                item["cost_status"] = "unpriced"
                results.append(item)
                write_json(root / "results.json", results)
                if not item["transport_success"]:
                    raise RuntimeError("视觉请求失败；已保存结果并停止整批，不换会话重试。")
        with SessionStore(work / "sessions") as store:
            all_events = store.load_events(sid)
            for item in (r for r in results if r["image"] == image_name):
                if item["transport_success"]:
                    request_id = item["answer"]["request_id"]
                    item["request_started"] = next(
                        e
                        for e in all_events
                        if e["type"] == "request_started"
                        and e["payload"]["request_id"] == request_id
                    )
                    item["request_finished"] = next(
                        e
                        for e in all_events
                        if e["type"] == "request_finished"
                        and e["payload"]["request_id"] == request_id
                    )
                    item["request_usage"] = next(
                        e
                        for e in all_events
                        if e["type"] == "request_usage" and e["payload"]["request_id"] == request_id
                    )
                    item["turn_completed"] = next(
                        e
                        for e in all_events
                        if e["type"] == "turn_completed"
                        and e["payload"]["turn_id"] == item["answer"]["turn_id"]
                    )
                item["session_jsonl"] = str(
                    next((work / "sessions").glob("*/*/*/" + sid + ".jsonl"))
                )
            write_json(work / "all_events.json", all_events)
        write_json(root / "results.json", results)
    metadata["finished_utc"] = datetime.now(UTC).isoformat()
    write_json(root / "runtime.json", metadata)
    print("Evidence: " + str(root / "results.json"), flush=True)


def main() -> None:
    """按显式阶段执行，视觉核图在 generate 和 run 两阶段之间完成。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("generate", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="adam-vision")
    parser.add_argument("--endpoint", default="http://127.0.0.1:8087/v1")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--process-record", type=Path)
    args = parser.parse_args()
    if args.stage == "generate":
        generate(args.output)
    else:
        run(
            args.output,
            LocalVisionConfig(args.model, args.endpoint, 180),
            manifest=args.manifest,
            process_record=args.process_record,
        )


if __name__ == "__main__":
    main()
