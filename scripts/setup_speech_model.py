"""显式准备公开Whisper-small固定权重；运行时仅使用本地文件。"""

import argparse
import importlib
import json
from pathlib import Path

REVISION = "536b0662742c02347bc0e980a01041f333bce120"


def main() -> None:
    """下载到用户指定目录并保存实际revision；不发送Hub账户凭据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    root = parser.parse_args().destination.resolve()
    hub = importlib.import_module("huggingface_hub")
    path = hub.snapshot_download(
        "Systran/faster-whisper-small",
        revision=REVISION,
        token=False,
        local_dir=root,
        allow_patterns=["*.json", "model.bin", "vocabulary.*", "README.md"],
        max_workers=4,
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {"repo": "Systran/faster-whisper-small", "revision": REVISION, "path": path}, indent=2
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
