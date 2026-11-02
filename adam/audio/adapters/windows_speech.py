"""固定Windows语音工作进程；文本经文件传递，取消回收进程及其音频设备。"""

import os
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

from adam.audio.input import SpeechInput
from adam.audio.models import Recording
from adam.contracts.cancellation import CancellationToken


class WindowsSpeech:
    """本地System.Speech合成和SoundPlayer播放；无云请求或自动重试。"""

    def __init__(self, *, voice: str = "Microsoft Huihui Desktop", timeout: float = 90) -> None:
        if not voice or len(voice) > 120 or not 0 < timeout <= 180:
            raise ValueError("invalid voice or timeout")
        self.voice, self.timeout = voice, timeout

    def synthesize(self, text: str, *, cancel: CancellationToken) -> bytes:
        """合成完整PCM WAV；文字限1500字，超过60秒或16MiB的输出拒绝播放。"""
        if not text.strip() or len(text) > 1500:
            raise ValueError("speech text must contain 1..1500 characters")
        with tempfile.TemporaryDirectory(prefix="adam-tts-") as temporary:
            root = Path(temporary)
            (root / "text.txt").write_text(text, encoding="utf-8")
            self._run("synthesize", root, cancel)
            path = root / "audio.wav"
            if path.stat().st_size > 16777216:
                raise ValueError("synthesized audio exceeds limit")
            return path.read_bytes()

    def play(self, audio: bytes, *, cancel: CancellationToken) -> None:
        """独立进程同步播放；返回或取消前确认进程退出，不影响其他应用音频。"""
        import io
        import wave

        with wave.open(io.BytesIO(audio), "rb") as wav:
            duration = wav.getnframes() * 1000 // wav.getframerate()
        SpeechInput._validate(Recording(audio, duration, "Windows TTS"))
        with tempfile.TemporaryDirectory(prefix="adam-playback-") as temporary:
            root = Path(temporary)
            (root / "audio.wav").write_bytes(audio)
            self._run("play", root, cancel)

    def _run(self, mode: str, root: Path, cancel: CancellationToken) -> None:
        """固定脚本和独立日志；取消/超时杀死且等待自己的worker，保留失败尾部。"""
        cancel.check()
        if os.name != "nt":
            raise RuntimeError("Windows speech requires Windows")
        powershell = (
            Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        with (root / "worker.log").open("w+b") as log:
            process = subprocess.Popen(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(Path(__file__).with_name("windows_speech.ps1")),
                    "-Mode",
                    mode,
                    "-Root",
                    str(root),
                    "-Voice",
                    self.voice,
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                deadline = monotonic() + self.timeout
                while process.poll() is None:
                    cancel.check()
                    if monotonic() > deadline:
                        raise TimeoutError("speech worker timed out")
                    cancel.wait(0.02)
                cancel.check()
                if process.returncode:
                    log.seek(max(0, log.seek(0, 2) - 3000))
                    raise RuntimeError(log.read().decode("utf-8", "replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
