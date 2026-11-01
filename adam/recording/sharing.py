"""显式导出供人工审阅的脱敏副本；不覆盖源录制，不创建可重放录制。"""

import hashlib
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adam.recording.format import RecordingError, RecordingLimits
from adam.recording.journal import DEFAULT_LIMITS, parse_recording
from adam.recording.redaction import RULES_VERSION, LiteralRedaction, redact_json

MAX_SHARE_BYTES = 64 * 1024 * 1024


def export_share(
    source: Path,
    output: Path,
    *,
    literals: tuple[LiteralRedaction, ...] = (),
    limits: RecordingLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """新建脱敏 JSON 及内嵌报告，返回无原文的摘要；来源和输出都不经过模型。

    只导出通过原录制校验的完整事件，残缺尾行不带出。来源摘要与内容绑定同一份读取；
    output 必须不存在，写入采用同目录临时文件加排他硬链接发布，失败不覆盖旧文件。
    结果始终标记待人工审阅和不可重放，规则命中不等于全部敏感信息已识别。
    """
    try:
        with source.open("rb") as file:
            raw = file.read(limits.max_bytes + 1)
        recording = parse_recording(raw, limits)
        events, findings = redact_json(list(recording.events), literals)
        summary = {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_bytes": len(raw),
            "source_complete": recording.complete,
            "source_torn_tail": recording.torn_tail,
            "event_count": len(recording.events),
            "replacement_count": len(findings),
            "counts": dict(Counter(item["category"] for item in findings)),
            "manual_review_required": True,
            "replayable": False,
        }
        payload = {
            "format": "adam_shared_recording_v1",
            "purpose": "human_review_only",
            "created_at": datetime.now(UTC).isoformat(),
            "summary": summary,
            "rules": {
                "version": RULES_VERSION,
                "literal_count": len(literals),
                "coverage": "bounded_patterns_and_explicit_literals_not_all_secrets",
            },
            "findings": findings,
            "events": events,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_SHARE_BYTES:
            raise RecordingError("脱敏分享副本超过 64 MiB，未导出。")
        _create_output(output, encoded)
        return summary
    except OSError as error:
        raise RecordingError(
            "分享副本未保存：来源不可读、输出已存在或目录无法排他写入。"
        ) from error


def _create_output(path: Path, raw: bytes) -> None:
    """临时文件只含脱敏内容；写入成功后排他发布，不覆盖同名文件或符号链接。"""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".adam-share-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            if file.write(raw) != len(raw):
                raise OSError("short share write")
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
