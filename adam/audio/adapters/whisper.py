"""固定离线Whisper工作进程；终止本次推理即可释放模型，不自动补请求。"""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

from adam.audio.models import Transcription
from adam.contracts.cancellation import CancellationToken


class WhisperASR:
    """固定本地Python/模型目录与语言，不接收远端URL或可执行命令。"""

    def __init__(
        self, python: Path, model: Path, *, language: str = "zh", timeout: float = 90
    ) -> None:
        if language not in {"zh", "en"} or not 0 < timeout <= 300:
            raise ValueError("invalid ASR language or timeout")
        self.python, self.model = python.resolve(), model.resolve()
        self.language, self.timeout = language, timeout

    def transcribe(self, audio: bytes, *, cancel: CancellationToken) -> Transcription:
        """单次离线识别；失败保留异常，取消/超时杀死本worker并等待退出。"""
        cancel.check()
        if not self.python.is_file() or not (self.model / "model.bin").is_file():
            raise ValueError("local ASR runtime or weights are missing")
        with tempfile.TemporaryDirectory(prefix="adam-asr-") as temporary:
            root = Path(temporary)
            (root / "input.wav").write_bytes(audio)
            output = root / "result.json"
            with (root / "error.log").open("w+b") as log:
                process = subprocess.Popen(
                    [
                        str(self.python),
                        str(Path(__file__).with_name("whisper_worker.py")),
                        str(root / "input.wav"),
                        str(output),
                        str(self.model),
                        self.language,
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    deadline = monotonic() + self.timeout
                    while process.poll() is None:
                        cancel.check()
                        if monotonic() > deadline:
                            raise TimeoutError("ASR timed out")
                        cancel.wait(0.05)
                    cancel.check()
                    if process.returncode:
                        log.seek(max(0, log.seek(0, 2) - 3000))
                        raise RuntimeError(log.read().decode("utf-8", "replace"))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            if output.stat().st_size > 65536:
                raise ValueError("ASR result exceeds limit")
            result = json.loads(output.read_text(encoding="utf-8"))
            text, tokens = result["text"], result["output_tokens"]
            if (
                not isinstance(text, str)
                or len(text) > 8000
                or type(tokens) is not int
                or tokens < 0
            ):
                raise ValueError("invalid ASR result")
            return Transcription(text.strip(), tokens)
