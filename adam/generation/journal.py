"""受保护的生成快照；沿用宿主持有的会话锁，恢复只读事实，不派发生成。"""

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.contracts.consumption import ConsumptionPlan, Quantities
from adam.contracts.media import MediaSource, decode_media_reference
from adam.generation.models import GenerationJob, GenerationSpec


def job_identity(value: str) -> str:
    """模型作业名只允许短ASCII标识，不作为任意文件路径。"""
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ValueError("invalid generation job id")
    return value


class GenerationJournal:
    """每个会话/能力独立目录，单宿主串行写；损坏记录阻止恢复，不跳过后重做。"""

    def __init__(self, root: Path, session_id: str, kind: str) -> None:
        self.root = root / job_identity(session_id) / kind
        self.session_id, self.kind = session_id, kind

    def save(self, job: GenerationJob) -> None:
        """同目录原子替换快照；失败向上传递，不将已有正文误报为已交付。"""
        if job.source.session_id != self.session_id or job.spec.kind != self.kind:
            raise ValueError("generation journal owner mismatch")
        target = self.root / (job_identity(job.job_id) + ".json")
        value = asdict(job)
        value["plan"] = job.plan.as_record()
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self) -> list[GenerationJob]:
        """有界读取全部作业并核对文件身份；临时文件不代表已提交的派发记录。"""
        paths = list(self.root.glob("*.json"))
        if len(paths) > 64:
            raise ValueError("generation journal capacity exceeded")
        jobs = []
        for path in paths:
            if path.stat().st_size > 65536:
                raise ValueError("generation snapshot exceeds limit")
            job = decode_job(json.loads(path.read_text(encoding="utf-8")))
            if (
                job.job_id != path.stem
                or job.source.session_id != self.session_id
                or job.spec.kind != self.kind
            ):
                raise ValueError("generation snapshot owner mismatch")
            jobs.append(job)
        return jobs


def decode_job(value: dict[str, Any]) -> GenerationJob:
    """还原完整契约和媒体引用；坏状态、用量或来源拒绝，不用默认值修补。"""
    spec = value["spec"]
    source = MediaSource(**value["source"])
    media = decode_media_reference(value["media"]) if value["media"] is not None else None
    if value["state"] not in {"running", "succeeded", "failed", "cancelled", "unknown"}:
        raise ValueError("invalid generation state")
    if type(value["cancel_requested"]) is not bool:
        raise ValueError("invalid generation cancellation flag")
    if media is not None and media.source != source:
        raise ValueError("generation media source mismatch")
    if value["state"] == "succeeded" and media is None:
        raise ValueError("successful generation has no media")
    return GenerationJob(
        **{
            **value,
            "job_id": job_identity(value["job_id"]),
            "spec": GenerationSpec(**{**spec, "parameters": tuple(map(tuple, spec["parameters"]))}),
            "source": source,
            "plan": ConsumptionPlan.from_record(value["plan"]),
            "usage": Quantities.from_record(value["usage"]),
            "media": media,
        }
    )
