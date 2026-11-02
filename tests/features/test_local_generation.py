"""本地后端只验证离线配置/参数与真实进程停止；模型产物由单独实机验收生成。"""

import subprocess
import sys
from pathlib import Path

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.generation.adapters.local_runtime import LocalRuntime
from adam.generation.adapters.musicgen import FILES as AUDIO_FILES
from adam.generation.adapters.musicgen import MusicGenBackend
from adam.generation.adapters.sd_turbo import FILES as IMAGE_FILES
from adam.generation.adapters.sd_turbo import SDTurboBackend
from adam.generation.models import GenerationSpec


@pytest.mark.parametrize(
    "kind,backend_type,names",
    [("image", SDTurboBackend, IMAGE_FILES), ("audio", MusicGenBackend, AUDIO_FILES)],
)
def test_offline_configuration_and_bounded_parameters(tmp_path, kind, backend_type, names):
    runtime = LocalRuntime(Path(sys.executable), tmp_path)
    backend = backend_type(runtime)
    with pytest.raises(ValueError, match="weights"):
        backend.prepare(GenerationSpec(kind, "test"))
    # 空文件仅用于确认派发前配置门槛；不调用generate，不充当真实模型或产物。
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    plan = backend.prepare(GenerationSpec(kind, "test"))
    assert plan.purpose == kind + "_generation" and plan.price is None
    assert plan.reservation.calls == 1
    with pytest.raises(ValueError, match="unsupported"):
        backend.prepare(GenerationSpec(kind, "test", (("command", "arbitrary"),)))
    invalid = (("width", 65),) if kind == "image" else (("duration_ms", 1001),)
    with pytest.raises(ValueError, match="divisible"):
        backend.prepare(GenerationSpec(kind, "test", invalid))


@pytest.mark.parametrize("cancelled", [False, True])
def test_timeout_or_cancel_kills_actual_worker_without_retry(tmp_path, monkeypatch, cancelled):
    original = subprocess.Popen
    processes = []
    token = CancellationToken()

    def start(args, **kwargs):
        # 替换推理边界为真实等待进程，验证宿主回收而不耗时加载GPU权重。
        process = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        if cancelled:
            token.cancel()
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    runtime = LocalRuntime(Path(sys.executable), tmp_path, timeout_seconds=0.1)
    expected = RequestInterrupted if cancelled else TimeoutError
    with pytest.raises(expected):
        runtime.run({"kind": "audio", "seed": 7, "max_input_tokens": 256}, cancel=token)
    assert len(processes) == 1 and processes[0].poll() is not None


def test_unconfigured_python_is_explicit_before_dispatch(tmp_path):
    backend = MusicGenBackend(LocalRuntime(tmp_path / "missing.exe", tmp_path))
    with pytest.raises(ValueError, match="Python"):
        backend.prepare(GenerationSpec("audio", "test"))
