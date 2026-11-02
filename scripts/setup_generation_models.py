"""显式下载固定公开模型；准备阶段与生成worker分离，不发送Hub账户凭据。"""

import argparse
import importlib
import json
from pathlib import Path

MODELS = (
    (
        "stabilityai/sd-turbo",
        "b261bac6fd2cf515557d5d0707481eafa0485ec2",
        "sd-turbo",
        (
            "model_index.json",
            "scheduler/*",
            "tokenizer/*",
            "*/config.json",
            "*.fp16.safetensors",
            "LICENSE.md",
            "README.md",
        ),
    ),
    (
        "facebook/musicgen-small",
        "4c8334b02c6ec4e8664a91979669a501ec497792",
        "musicgen-small",
        ("*.json", "model.safetensors", "spiece.model", "README.md"),
    ),
)


def main() -> None:
    """将用户指定目录作为模型缓存，逐项保存真实仓库版本和实际本地路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = args.destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    hub = importlib.import_module("huggingface_hub")
    manifest = []
    for repo, revision, folder, patterns in MODELS:
        path = hub.snapshot_download(
            repo,
            revision=revision,
            local_dir=root / folder,
            allow_patterns=list(patterns),
            token=False,
            max_workers=4,
        )
        manifest.append({"repo": repo, "revision": revision, "path": str(path)})
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(repo + " ready", flush=True)


if __name__ == "__main__":
    main()
