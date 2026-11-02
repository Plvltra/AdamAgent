"""隔离运行固定生成worker；参数走JSON，取消或超时回收本次进程。"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from time import monotonic
from typing import Any

from adam.contracts.cancellation import CancellationToken


class LocalRuntime:
    """显式Python与权重目录；不下载、不读取API密钥、不调用任意宿主命令。"""

    def __init__(self, python: Path, model_dir: Path, *, timeout_seconds: float = 180) -> None:
        if not 0 < timeout_seconds <= 600:
            raise ValueError("invalid local generation timeout")
        self.python = python.resolve()
        self.model_dir = model_dir.resolve()
        self.timeout_seconds = timeout_seconds

    def require_files(self, names: tuple[str, ...]) -> None:
        """派发前确认运行时和固定模型文件存在；缺失明确失败，不产生替代产物。"""
        if not self.python.is_file():
            raise ValueError("local generation Python is not configured")
        if any(not (self.model_dir / name).is_file() for name in names):
            raise ValueError("local generation weights are not configured")

    def run(
        self, payload: dict[str, Any], *, cancel: CancellationToken
    ) -> tuple[bytes, dict[str, Any]]:
        """执行固定worker一次，最多读取32MiB产物；失败日志有界，停止不重启进程。"""
        cancel.check()
        with tempfile.TemporaryDirectory(prefix="adam-generation-") as directory:
            root = Path(directory)
            request = root / "request.json"
            request.write_text(
                json.dumps({**payload, "model_dir": str(self.model_dir)}), encoding="utf-8"
            )
            env = {
                **os.environ,
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            with (root / "worker.log").open("w+b") as log:
                process = subprocess.Popen(
                    [str(self.python), "-m", "adam.generation.worker", str(request), str(root)],
                    cwd=Path(__file__).resolve().parents[3],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    deadline = monotonic() + self.timeout_seconds
                    while process.poll() is None:
                        cancel.check()
                        if monotonic() >= deadline:
                            raise TimeoutError("local generation timed out")
                        cancel.wait(0.05)
                    cancel.check()
                    if process.returncode:
                        log.seek(max(0, log.seek(0, 2) - 4000))
                        raise RuntimeError(
                            "local generation failed: " + log.read().decode("utf-8", "replace")
                        )
                except BaseException as error:
                    if process.poll() is None:
                        process.kill()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            error.add_note("生成worker未在回收期限内退出。")
                    raise
            metadata = root / "result.json"
            artifact = root / "artifact"
            if metadata.stat().st_size > 8192 or not 0 < artifact.stat().st_size <= 33554432:
                raise ValueError("local generation output exceeds limit")
            report = json.loads(metadata.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or report.get("kind") != payload["kind"]:
                raise ValueError("local generation report mismatch")
            if report.get("seed") != payload["seed"]:
                raise ValueError("local generation seed mismatch")
            tokens = report.get("input_tokens")
            if type(tokens) is not int or not 0 < tokens <= payload["max_input_tokens"]:
                raise ValueError("invalid local generation input usage")
            return artifact.read_bytes(), report


def integer_parameters(
    values: dict[str, Any], bounds: dict[str, tuple[int, int, int]]
) -> dict[str, int]:
    """仅接受本适配器公开的整数参数，不将模型提供的字段转换为CLI选项。"""
    if values.keys() - bounds.keys():
        raise ValueError("unsupported generation parameter")
    result = {}
    for name, (default, minimum, maximum) in bounds.items():
        value = values.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("invalid generation parameter: " + name)
        result[name] = value
    return result
