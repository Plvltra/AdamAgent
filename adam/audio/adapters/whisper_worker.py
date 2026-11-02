"""固定Whisper CPU worker；输出原始转写与模型原生输出token数。"""

import importlib
import json
import sys
from pathlib import Path


def main() -> None:
    """只使用本地模型，固定单束解码与语言，不下载、不按转写内容执行命令。"""
    audio, output, model_path, language = sys.argv[1:]
    model = importlib.import_module("faster_whisper").WhisperModel(
        model_path, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True
    )
    segments, _ = model.transcribe(
        audio, language=language, beam_size=1, temperature=0, condition_on_previous_text=False
    )
    rows = list(segments)
    result = {
        "text": "".join(row.text for row in rows),
        "output_tokens": sum(len(row.tokens) for row in rows),
    }
    Path(output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
