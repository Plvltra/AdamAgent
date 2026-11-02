"""SD-Turbo单图适配器；派发前校验离线文件，完整解码后才交付PNG。"""

import io
from secrets import randbits

from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import ConsumptionPlan, Quantities
from adam.generation.adapters.local_runtime import LocalRuntime, integer_parameters
from adam.generation.models import GeneratedMedia, GenerationSpec

FILES = (
    "model_index.json",
    "scheduler/scheduler_config.json",
    "tokenizer/vocab.json",
    "tokenizer/merges.txt",
    "text_encoder/config.json",
    "text_encoder/model.fp16.safetensors",
    "unet/config.json",
    "unet/diffusion_pytorch_model.fp16.safetensors",
    "vae/config.json",
    "vae/diffusion_pytorch_model.fp16.safetensors",
)


class SDTurboBackend:
    """固定SD-Turbo、1～4步和CFG=0；种子固定随机源，不保证跨设备逐像素一致。"""

    name = "local-sd-turbo"
    supports_seed = True

    def __init__(self, runtime: LocalRuntime) -> None:
        self.runtime = runtime

    def prepare(self, spec: GenerationSpec) -> ConsumptionPlan:
        """本地检查权重与参数，预留一张图片和最多77个提示token，价格未知。"""
        self._parameters(spec)
        self.runtime.require_files(FILES)
        return ConsumptionPlan(Quantities(input_tokens=77, images=1, calls=1), "image_generation")

    def generate(self, spec: GenerationSpec, *, cancel: CancellationToken) -> GeneratedMedia:
        """生成一次并核对PNG尺寸与完整像素；不以格式通过代替画面质量验收。"""
        from PIL import Image

        params = self._parameters(spec)
        seed = spec.seed if spec.seed is not None else randbits(32)
        data, report = self.runtime.run(
            {
                "kind": "image",
                "prompt": spec.prompt,
                "seed": seed,
                "parameters": params,
                "max_input_tokens": 77,
            },
            cancel=cancel,
        )
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or image.size != (params["width"], params["height"]):
                raise ValueError("generated image dimensions or format mismatch")
            image.load()
        cancel.check()
        return GeneratedMedia(
            data,
            "image/png",
            Quantities(input_tokens=report["input_tokens"], images=1, calls=1),
            seed,
        )

    def _parameters(self, spec: GenerationSpec) -> dict[str, int]:
        """限制图片像素与步数；不能将未知参数传入推理管线。"""
        if spec.kind != "image":
            raise ValueError("SD-Turbo requires image generation")
        params = integer_parameters(
            dict(spec.parameters),
            {"width": (512, 64, 512), "height": (512, 64, 512), "steps": (1, 1, 4)},
        )
        if params["width"] % 8 or params["height"] % 8:
            raise ValueError("image dimensions must be divisible by eight")
        return params
