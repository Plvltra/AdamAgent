"""生成任务的同步宿主入口；身份先留证，失败不会自动重提或重复发布。"""

import re
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from threading import RLock

from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionSnapshot, Quantities
from adam.contracts.errors import RequestInterrupted
from adam.contracts.media import MediaRef, MediaSource, validate_mime_type
from adam.generation.models import (
    GeneratedMedia,
    GenerationBackend,
    GenerationBudget,
    GenerationJob,
    GenerationSpec,
)

UNKNOWN_USAGE = Quantities(None, None, None, None, 1)
UNCONFIRMED_USAGE = Quantities(None, None, None, None, None)


class GenerationJobs:
    """单宿主持有最多64个作业；同步生成期间可从另一线程查询或请求取消。

    save须原子替换同ID快照，不得触发生成；宿主重建后显式restore快照。
    回调负责真实来源权限与存储，生成包只通过原媒体发布/共享额度端口工作。
    """

    def __init__(
        self,
        backend: GenerationBackend,
        publish: Callable[[MediaSource, bytes, str], MediaRef],
        budget: GenerationBudget,
        save: Callable[[GenerationJob], None],
        *,
        max_bytes: int = 33554432,
    ) -> None:
        if not backend.name or len(backend.name) > 128 or not 0 < max_bytes <= 134217728:
            raise ValueError("invalid generation backend or byte limit")
        self.backend, self.publish, self.budget, self.save = backend, publish, budget, save
        self.max_bytes = max_bytes
        self._jobs: dict[str, GenerationJob] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = RLock()

    def submit(
        self,
        job_id: str,
        spec: GenerationSpec,
        source: MediaSource,
        *,
        cancel: CancellationToken,
    ) -> GenerationJob:
        """预留并保存身份后尝试一次；异常继续抛出，最终快照可用get读取。

        未派发取消是cancelled且零消费；派发后无可靠结果是unknown并阻止后续额度。
        已知产物在发布前取消可停止交付，但已发生用量仍结算；发布/记录失败不重做生成。
        """
        cancel.check()
        with self._lock:
            self._require_new(job_id)
        if spec.seed is not None and not self.backend.supports_seed:
            raise ValueError("backend does not support a seed")
        plan = self.backend.prepare(spec)
        if plan.purpose != spec.kind + "_generation":
            raise ValueError("generation consumption purpose mismatch")
        with self.budget.reserve(plan.reservation):
            with self._lock:
                self._require_new(job_id)
                job = GenerationJob(
                    job_id, self.backend.name, spec, source, plan, "running", UNCONFIRMED_USAGE
                )
                self._jobs[job_id] = job
                self._tokens[job_id] = cancel
            attempted, reliable = False, False
            stage = "start_record"
            try:
                self._record(job)
                cancel.check()
                stage = "backend"
                attempted = True
                result = self.backend.generate(spec, cancel=cancel)
                plan.settlement(result.usage)
                reliable = True
                job = replace(job, usage=result.usage, seed_used=result.seed_used)
                cancel.check()
                stage = "output_validation"
                self._validate_output(spec, result)
                stage = "publication"
                reference = self.publish(source, result.data, result.mime_type)
                if (
                    reference.source != source
                    or reference.mime_type != result.mime_type
                    or reference.version != sha256(result.data).hexdigest()
                    or reference.size_bytes != len(result.data)
                ):
                    raise ValueError("published generation evidence mismatch")
                job = replace(job, state="succeeded", media=reference)
                stage = "finish_record"
                return self._record(job)
            except BaseException as error:
                unknown = attempted and (not reliable or stage in {"publication", "finish_record"})
                state = (
                    "unknown"
                    if unknown
                    else (
                        "cancelled"
                        if isinstance(error, (RequestInterrupted, KeyboardInterrupt))
                        else "failed"
                    )
                )
                job = replace(
                    job,
                    state=state,
                    usage=(
                        Quantities()
                        if not attempted
                        else UNKNOWN_USAGE
                        if not reliable
                        else job.usage
                    ),
                    cancel_requested=cancel.cancelled,
                    error=stage + ":" + type(error).__name__,
                )
                try:
                    self._record(job)
                except Exception:
                    error.add_note("生成状态保存失败；保留本地快照，不要重新生成。")
                raise
            finally:
                with self._lock:
                    self._tokens.pop(job_id, None)

    def get(self, job_id: str) -> GenerationJob:
        """只读已有快照，不查询供应商、不触发生成，也不推断未知状态。"""
        with self._lock:
            return self._jobs[job_id]

    def cancel(self, job_id: str) -> GenerationJob:
        """请求正在运行的协作取消，终态不变；请求成功不代表远端撤销或退款。"""
        with self._lock:
            token = self._tokens.get(job_id)
            job = self._jobs[job_id]
            if token is None:
                return job
            token.cancel()
            return self._record(replace(job, cancel_requested=True))

    def restore(self, job: GenerationJob) -> GenerationJob:
        """恢复宿主可信快照及其额度；遗留running转unknown，不重问或重新派发。"""
        with self._lock:
            self._require_new(job.job_id)
            if job.backend != self.backend.name:
                raise ValueError("generation backend changed")
            if job.state == "running":
                job = replace(
                    job, state="unknown", usage=UNCONFIRMED_USAGE, error="interrupted_host"
                )
            return self._record(job)

    def _record(self, job: GenerationJob) -> GenerationJob:
        """先保留内存事实和原额度快照，再交宿主落盘；同一身份刷新不重复计费。"""
        with self._lock:
            previous = self._jobs.get(job.job_id)
            if previous is not None and previous.cancel_requested:
                job = replace(job, cancel_requested=True)
            self._jobs[job.job_id] = job
            amounts = job.usage.as_record()
            snapshot = ConsumptionSnapshot(
                Quantities(**{unit: count or 0 for unit, count in amounts.items()}),
                tuple(unit for unit, count in amounts.items() if count is None),
            )
            # 在途额度由reserve持有，未知占位不是已发生消费；终态才替换实际快照。
            if job.state != "running":
                self.budget.update(
                    f"generation:{job.source.session_id}:{job.backend}:{job.job_id}", snapshot
                )
            self.save(job)
            return job

    def _require_new(self, job_id: str) -> None:
        """拒绝重复关联及无界累积；成功、失败、取消和未知ID均不可再次提交。"""
        if (
            re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id) is None
            or job_id in self._jobs
            or len(self._jobs) >= 64
        ):
            raise ValueError("generation job id is invalid, duplicate or capacity exceeded")

    def _validate_output(self, spec: GenerationSpec, result: GeneratedMedia) -> None:
        """仅检查契约、字节上限与种子回执；不把MIME声明当作格式或质量验收。"""
        validate_mime_type(result.mime_type)
        if (
            not isinstance(result.data, bytes)
            or not 0 < len(result.data) <= self.max_bytes
            or not result.mime_type.startswith(spec.kind + "/")
            or (spec.seed is not None and result.seed_used != spec.seed)
        ):
            raise ValueError("invalid generated artifact or seed receipt")
