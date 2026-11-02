"""仅由本地适配器启动的离线CUDA推理worker；每个进程只生成一个产物。"""

import importlib
import json
import sys
import wave
from pathlib import Path
from types import ModuleType
from typing import Any


def _torch(seed: int) -> ModuleType:
    """显式要求CUDA并固定本进程随机源；不自动改用CPU或联网下载。"""
    torch = importlib.import_module("torch")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA generation runtime is unavailable")
    torch.set_num_threads(4)
    torch.manual_seed(seed)
    return torch


def generate_image(request: dict[str, Any], target: Path) -> dict[str, Any]:
    """使用官方SD-Turbo离线管线生成PNG；拒绝超长提示而非静默截断。"""
    torch = _torch(request["seed"])
    pipeline_type = importlib.import_module("diffusers").StableDiffusionPipeline
    pipeline = pipeline_type.from_pretrained(
        request["model_dir"],
        torch_dtype=torch.float16,
        variant="fp16",
        use_safetensors=True,
        local_files_only=True,
    ).to("cuda")
    tokens = pipeline.tokenizer(request["prompt"], truncation=False)["input_ids"]
    if len(tokens) > request["max_input_tokens"]:
        raise ValueError("image prompt exceeds local encoder token limit")
    params = request["parameters"]
    with torch.inference_mode():
        image = pipeline(
            prompt=request["prompt"],
            width=params["width"],
            height=params["height"],
            num_inference_steps=params["steps"],
            guidance_scale=0.0,
            generator=torch.Generator(device="cuda").manual_seed(request["seed"]),
        ).images[0]
    image.save(target, format="PNG")
    return {"input_tokens": len(tokens), "width": image.width, "height": image.height}


def generate_audio(request: dict[str, Any], target: Path) -> dict[str, Any]:
    """由MusicGen实际音频码解码为PCM；裁掉末尾多余码步，不填充伪造缺失音频。"""
    torch = _torch(request["seed"])
    numpy = importlib.import_module("numpy")
    transformers = importlib.import_module("transformers")
    processor = transformers.AutoProcessor.from_pretrained(
        request["model_dir"], local_files_only=True, trust_remote_code=False
    )
    model = transformers.MusicgenForConditionalGeneration.from_pretrained(
        request["model_dir"],
        local_files_only=True,
        use_safetensors=True,
        torch_dtype=torch.float32,
        attn_implementation="eager",
    ).to("cuda")
    inputs = processor(
        text=[request["prompt"]], padding=True, return_tensors="pt", truncation=False
    )
    count = int(inputs["input_ids"].shape[-1])
    if count > request["max_input_tokens"]:
        raise ValueError("audio prompt exceeds bounded text encoder input")
    inputs = {key: value.to("cuda") for key, value in inputs.items()}
    duration = request["parameters"]["duration_ms"]
    with torch.inference_mode():
        values = model.generate(
            **inputs, do_sample=True, guidance_scale=3.0, max_new_tokens=duration // 20 + 4
        )
    if tuple(values.shape[:2]) != (1, 1) or int(model.config.audio_encoder.sampling_rate) != 32000:
        raise ValueError("unexpected audio model output shape")
    samples = values[0, 0].float().cpu().numpy()
    required = duration * 32
    if len(samples) < required or not numpy.isfinite(samples).all():
        raise ValueError("generated audio incomplete or nonfinite")
    samples = samples[:required]
    if float(numpy.max(numpy.abs(samples))) < 0.000001:
        raise ValueError("generated audio is silent")
    pcm = (numpy.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(32000)
        output.writeframes(pcm.tobytes())
    return {"input_tokens": count, "audio_ms": duration, "sample_rate": 32000}


def main() -> None:
    """固定本地文件协议，不接受模型传来的可执行程序或远端模型地址。"""
    request_path, output = Path(sys.argv[1]), Path(sys.argv[2])
    if request_path.stat().st_size > 65536:
        raise ValueError("generation worker request too large")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not Path(request["model_dir"]).is_dir():
        raise ValueError("local model directory is missing")
    if request["kind"] == "image":
        report = generate_image(request, output / "artifact")
    elif request["kind"] == "audio":
        report = generate_audio(request, output / "artifact")
    else:
        raise ValueError("unsupported generation kind")
    report.update({"kind": request["kind"], "seed": request["seed"]})
    (output / "result.json").write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main()
