"""主宿主显式生成工具；沿用原工具来源、媒体发布和注入的C00额度。"""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import quote
from adam.contracts.errors import ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.media import MediaSource
from adam.contracts.models import ExecutionRecorder
from adam.generation.journal import GenerationJournal, job_identity
from adam.generation.models import (
    GenerationBackend,
    GenerationBudget,
    GenerationJob,
    GenerationSpec,
)
from adam.generation.service import GenerationJobs
from adam.session.media_evidence import SessionMedia
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry

IDENTITY = {
    "kind": ("string", "image或audio，必须是宿主启用的能力"),
    "job_id": ("string", "本会话内固定作业名；重复通知复用此ID，不能换ID自动重试"),
}
SPECS = {
    "generate_media": schema(
        "按用户请求生成一份图片或短音频并返回原媒体引用。同步等待；失败或未知不能自动重试。"
        "相同ID返回原结果，不再生成；费用未知保持未知，格式成功不证明质量。",
        {
            **IDENTITY,
            "prompt": ("string", "生成内容提示"),
            "seed": ("integer", "可选固定32位非负种子"),
            "width": ("integer", "仅图片，像素宽"),
            "height": ("integer", "仅图片，像素高"),
            "steps": ("integer", "仅图片，推理步数"),
            "duration_ms": ("integer", "仅音频，毫秒时长"),
        },
        ["kind", "job_id", "prompt"],
    ),
    "generation_result": schema(
        "只读本会话已有生成作业和原媒体引用；不轮询远端、不重新生成。",
        IDENTITY,
        ["kind", "job_id"],
    ),
}


class GenerationTools:
    """只注册主宿主提供的固定后端；模型不能设置模型目录、Python或媒体所有者。"""

    def __init__(
        self,
        backends: dict[str, GenerationBackend],
        budget: GenerationBudget,
        media: SessionMedia,
        root: Path,
    ) -> None:
        if not backends or backends.keys() - {"image", "audio"}:
            raise ValueError("generation backends require image or audio keys")
        self.backends, self.budget, self.media, self.root = dict(backends), budget, media, root
        self._source: dict[str, str] | None = None
        self._jobs: dict[tuple[str, str], GenerationJobs] = {}

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        """串行注册写会话能力；结果容量不足时在派发前拒绝，避免交付引用被截断。"""
        if registry.limits.max_result_chars < 4000:
            raise ValueError("generation tools require at least 4000 result characters")
        return registry.extended(
            SPECS,
            {"generate_media": self.direct, "generation_result": self.query},
            capabilities={name: ToolCapability.SESSION_STATE for name in SPECS},
            interruptible_handlers={"generate_media": self.generate},
            groups={name: "generation" for name in SPECS},
        )

    def bind_source(self, source: dict[str, str]) -> None:
        """由原执行器绑定真实工具调用，派发前再核对活动会话。"""
        self._source = dict(source)

    def direct(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """无独立取消入口时仍使用相同来源和单次派发流程。"""
        return self.generate(args, record, CancellationToken())

    def generate(
        self, args: dict[str, Any], record: ExecutionRecorder | None, cancel: CancellationToken
    ) -> dict[str, Any]:
        """先恢复同会话快照；重复ID只返回原事实，变更提示须显式使用新作业。"""
        cancel.check()
        source = self._current_source("generate_media")
        jobs = self._service(source.session_id, args["kind"])
        identity = job_identity(args["job_id"])
        spec = GenerationSpec(
            args["kind"],
            args["prompt"],
            tuple(
                (key, args[key])
                for key in ("width", "height", "steps", "duration_ms")
                if key in args
            ),
            args.get("seed"),
        )
        try:
            previous = jobs.get(identity)
        except KeyError:
            previous = None
        if previous is not None:
            if previous.spec != spec:
                raise ToolError("作业ID已绑定其他请求，未重新生成。")
            return self._result(previous, duplicate=True)
        try:
            job = jobs.submit(identity, spec, source, cancel=cancel)
        except (OSError, RuntimeError, ValueError) as error:
            try:
                job = jobs.get(identity)
            except KeyError:
                raise ToolError(str(error)) from error
        return self._result(job)

    def query(self, args: dict[str, Any], record: ExecutionRecorder | None) -> dict[str, Any]:
        """查询只认当前宿主会话；有媒体时原读取器复核来源及字节版本。"""
        source = self._current_source("generation_result")
        jobs = self._service(source.session_id, args["kind"])
        try:
            job = jobs.get(job_identity(args["job_id"]))
        except KeyError as error:
            raise ToolError("当前会话没有该生成作业。") from error
        return self._result(job)

    def _current_source(self, name: str) -> MediaSource:
        """生成前核对真实工具来源，禁止直接调用伪造生成授权或跨会话查询。"""
        sid = self.media.store.active_session_id
        if sid is None or self._source is None:
            raise ToolError("生成工具需要活动会话及真实工具来源。")
        source = MediaSource(sid, kind="tool", tool_name=name, **self._source)
        self.media.store.validate_media_source(source)
        return source

    def _service(self, sid: str, kind: str) -> GenerationJobs:
        """先恢复本会话所有已存能力，避免切换模态绕过未知消费；损坏或撤销后端须人工处理。"""
        if kind not in self.backends:
            raise ToolError("该生成能力未配置。")
        key = (sid, kind)
        if key not in self._jobs:
            saved = (
                {path.name for path in (self.root / sid).iterdir()}
                if (self.root / sid).exists()
                else set()
            )
            if saved - self.backends.keys():
                raise ToolError("存在当前未配置后端的生成记录，无法确认全部消费。")
            restored = {}
            for mode, backend in self.backends.items():
                journal = GenerationJournal(self.root, sid, mode)
                jobs = GenerationJobs(backend, self.media.publish, self.budget, journal.save)
                for job in journal.load():
                    jobs.restore(job)
                restored[(sid, mode)] = jobs
            self._jobs.update(restored)
        return self._jobs[key]

    def _result(self, job: GenerationJob, *, duplicate: bool = False) -> dict[str, Any]:
        """交付有界状态与完整原媒体引用；不把媒体容器路径冒充可播放文件。"""
        if job.media is not None:
            self.media.read(job.media.media_id, expected_version=job.media.version)
        return {
            "ok": job.state == "succeeded",
            "job_id": job.job_id,
            "state": job.state,
            "duplicate": duplicate,
            "media": asdict(job.media) if job.media else None,
            "usage": job.usage.as_record(),
            "charge": quote(job.usage, job.plan.price),
            "seed_used": job.seed_used,
            "error": job.error,
        }
