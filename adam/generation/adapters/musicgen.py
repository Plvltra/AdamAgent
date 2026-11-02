"""MusicGen短音乐适配器；音频毫秒由真实PCM帧数计算，不按文本token估算。"""

import io
import wave
from secrets import randbits

from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionPlan, Quantities
from adam.generation.adapters.local_runtime import LocalRuntime, integer_parameters
from adam.generation.models import GeneratedMedia, GenerationSpec

FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


class MusicGenBackend:
    """固定MusicGen-small生成1～5秒单声道音乐；不是C05回答朗读或仿声。"""

    name = "local-musicgen-small"
    supports_seed = True

    def __init__(self, runtime: LocalRuntime) -> None:
        self.runtime = runtime

    def prepare(self, spec: GenerationSpec) -> ConsumptionPlan:
        """预留显式音频长度、提示token和一次调用；离线缺文件时不派发。"""
        params = self._parameters(spec)
        self.runtime.require_files(FILES)
        return ConsumptionPlan(
            Quantities(input_tokens=256, audio_ms=params["duration_ms"], calls=1),
            "audio_generation",
        )

    def generate(self, spec: GenerationSpec, *, cancel: CancellationToken) -> GeneratedMedia:
        """核对完整PCM、长度和采样格式；可播放不代表音乐符合提示。"""
        params = self._parameters(spec)
        seed = spec.seed if spec.seed is not None else randbits(32)
        data, report = self.runtime.run(
            {
                "kind": "audio",
                "prompt": spec.prompt,
                "seed": seed,
                "parameters": params,
                "max_input_tokens": 256,
            },
            cancel=cancel,
        )
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            if (
                audio.getframerate() != 32000
                or audio.getnchannels() != 1
                or audio.getsampwidth() != 2
            ):
                raise ValueError("generated audio format mismatch")
            if frames != params["duration_ms"] * 32 or len(audio.readframes(frames)) != frames * 2:
                raise ValueError("generated audio duration or payload mismatch")
        cancel.check()
        return GeneratedMedia(
            data,
            "audio/wav",
            Quantities(input_tokens=report["input_tokens"], audio_ms=frames // 32, calls=1),
            seed,
        )

    def _parameters(self, spec: GenerationSpec) -> dict[str, int]:
        """仅开放毫秒时长，按20ms音频码步长限制，禁止任意模型参数。"""
        if spec.kind != "audio":
            raise ValueError("MusicGen requires audio generation")
        params = integer_parameters(dict(spec.parameters), {"duration_ms": (3000, 1000, 5000)})
        if params["duration_ms"] % 20:
            raise ValueError("audio duration must be divisible by twenty milliseconds")
        return params
