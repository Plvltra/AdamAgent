"""生成作业经过真实媒体存储与C00额度；后端只返回明确的音频fixture，不验收生成质量。"""

import json
from dataclasses import asdict, replace
from threading import Event, Thread

import pytest

from adam.application.subagents.unit_budget import UnitBudget
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionPlan, Quantities, RateCard, quote
from adam.contracts.errors import BudgetError, RequestInterrupted
from adam.generation.models import GeneratedMedia, GenerationSpec
from adam.generation.service import GenerationJobs
from tests.features.test_media_evidence import active_media, wav

SPEC = GenerationSpec("audio", "A short bell", (("duration_ms", 100),), seed=7)


class Backend:
    name = "fixture-audio"
    supports_seed = True

    def __init__(self, action=None):
        self.action, self.calls = action, 0

    def prepare(self, spec):
        return ConsumptionPlan(
            Quantities(audio_ms=100, calls=1),
            "audio_generation",
            RateCard("USD", (("audio_ms", "0.001"), ("calls", "0.02"))),
        )

    def generate(self, spec, *, cancel):
        self.calls += 1
        if self.action:
            return self.action(spec, cancel)
        return GeneratedMedia(wav(), "audio/wav", Quantities(audio_ms=100, calls=1), spec.seed)


def budget(calls=3):
    return UnitBudget(Quantities(None, None, None, 300, calls))


def journal(root):
    """模拟宿主原子作业快照端口；真实JSON保留状态、用量和引用，绝不启动生成。"""

    def save(job):
        target = root / (job.job_id + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(job), ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)

    return save


def test_success_publishes_real_reference_and_settles_once(tmp_path):
    backend, quota = Backend(), budget()
    with active_media(tmp_path) as (_, source, media):
        jobs = GenerationJobs(backend, media.publish, quota, journal(tmp_path))
        job = jobs.submit("bell", SPEC, source, cancel=CancellationToken())
        assert job.state == "succeeded" and job.seed_used == 7
        assert job.media.source == source
        assert media.read(job.media.media_id, expected_version=job.media.version).data == wav()
        assert quote(job.usage, job.plan.price)["cost"] == "0.12"
        assert quota.usage().known == Quantities(audio_ms=100, calls=1)
        assert (
            json.loads((tmp_path / "bell.json").read_text())["media"]["media_id"]
            == job.media.media_id
        )
        assert jobs.cancel("bell") == job
        with pytest.raises(ValueError, match="duplicate"):
            jobs.submit("bell", SPEC, source, cancel=CancellationToken())
        assert backend.calls == 1


def test_budget_and_unsupported_seed_reject_before_dispatch(tmp_path):
    backend = Backend()
    with active_media(tmp_path) as (_, source, media):
        jobs = GenerationJobs(backend, media.publish, budget(0), journal(tmp_path))
        with pytest.raises(BudgetError):
            jobs.submit("quota", SPEC, source, cancel=CancellationToken())
        backend.supports_seed = False
        with pytest.raises(ValueError, match="seed"):
            jobs.submit("seed", SPEC, source, cancel=CancellationToken())
        assert backend.calls == 0 and not list(tmp_path.glob("*.json"))


def test_timeout_and_restore_keep_unknown_without_resubmission(tmp_path):
    def lost(spec, cancel):
        raise TimeoutError("injected lost response after dispatch")

    backend, quota = Backend(lost), budget()
    with active_media(tmp_path) as (_, source, media):
        jobs = GenerationJobs(backend, media.publish, quota, journal(tmp_path))
        with pytest.raises(TimeoutError):
            jobs.submit("lost", SPEC, source, cancel=CancellationToken())
        job = jobs.get("lost")
        assert job.state == "unknown" and job.media is None
        assert quota.usage().known.calls == 1 and quota.usage().unknown_units
        restored = GenerationJobs(backend, media.publish, budget(), journal(tmp_path))
        restored.restore(job)
        with pytest.raises(ValueError, match="duplicate"):
            restored.submit("lost", SPEC, source, cancel=CancellationToken())
        with pytest.raises(BudgetError):
            restored.submit("another", SPEC, source, cancel=CancellationToken())
        assert backend.calls == 1
        assert json.loads((tmp_path / "lost.json").read_text())["state"] == "unknown"


def test_running_snapshot_restores_as_unknown_without_invocation(tmp_path):
    with active_media(tmp_path) as (_, source, media):
        backend = Backend()
        jobs = GenerationJobs(backend, media.publish, budget(), journal(tmp_path))
        complete = jobs.submit("original", SPEC, source, cancel=CancellationToken())
        recovering = GenerationJobs(backend, media.publish, budget(), journal(tmp_path))
        recovered = recovering.restore(replace(complete, state="running", media=None))
        assert recovered.state == "unknown" and recovered.usage.audio_ms is None
        assert recovered.usage.calls is None
        assert backend.calls == 1


def test_cancel_during_backend_keeps_unknown_charge_and_releases_reservation(tmp_path):
    entered, errors = Event(), []

    def waiting(spec, cancel):
        entered.set()
        while True:
            cancel.wait(0.01)

    backend, quota = Backend(waiting), budget()
    with active_media(tmp_path) as (_, source, media):
        jobs = GenerationJobs(backend, media.publish, quota, journal(tmp_path))

        def run():
            try:
                jobs.submit("cancel", SPEC, source, cancel=CancellationToken())
            except BaseException as error:
                errors.append(error)

        worker = Thread(target=run)
        worker.start()
        try:
            assert entered.wait(2)
            assert jobs.get("cancel").state == "running"
            jobs.cancel("cancel")
        finally:
            if entered.is_set():
                jobs.cancel("cancel")
            worker.join(2)
        assert not worker.is_alive() and isinstance(errors[0], RequestInterrupted)
        job = jobs.get("cancel")
        assert job.cancel_requested and job.state == "unknown" and job.media is None
        assert quota.as_record()["reserved"] == Quantities().as_record()
        assert backend.calls == 1


def test_cancel_after_known_generation_keeps_consumed_units(tmp_path):
    def finish_then_cancel(spec, cancel):
        cancel.cancel()
        return GeneratedMedia(wav(), "audio/wav", Quantities(audio_ms=100, calls=1), spec.seed)

    with active_media(tmp_path) as (store, source, media):
        jobs = GenerationJobs(
            Backend(finish_then_cancel), media.publish, budget(), journal(tmp_path)
        )
        with pytest.raises(RequestInterrupted):
            jobs.submit("cancelled", SPEC, source, cancel=CancellationToken())
        job = jobs.get("cancelled")
        assert job.state == "cancelled" and job.usage.audio_ms == 100
        assert quote(job.usage, job.plan.price)["cost"] == "0.12"
        assert not any(e["type"] == "media_saved" for e in store.load_events(source.session_id))


@pytest.mark.parametrize("boundary", ["start_record", "publication", "finish_record"])
def test_storage_failures_never_repeat_generation(tmp_path, boundary):
    backend, quota = Backend(), budget()
    with active_media(tmp_path) as (store, source, media):

        def save(job):
            if boundary == "start_record" or (
                boundary == "finish_record" and job.state == "succeeded"
            ):
                raise OSError("injected snapshot failure")
            journal(tmp_path)(job)

        def publish(owner, data, mime):
            reference = media.publish(owner, data, mime)
            if boundary == "publication":
                raise OSError("injected failure after media publication")
            return reference

        jobs = GenerationJobs(backend, publish, quota, save)
        with pytest.raises(OSError):
            jobs.submit("storage", SPEC, source, cancel=CancellationToken())
        job = jobs.get("storage")
        assert job.state == ("failed" if boundary == "start_record" else "unknown")
        assert backend.calls == (0 if boundary == "start_record" else 1)
        assert job.usage.calls == backend.calls
        assert (job.media is not None) == (boundary == "finish_record")
        with pytest.raises(ValueError, match="duplicate"):
            jobs.submit("storage", SPEC, source, cancel=CancellationToken())
        count = sum(e["type"] == "media_saved" for e in store.load_events(source.session_id))
        assert count == backend.calls
