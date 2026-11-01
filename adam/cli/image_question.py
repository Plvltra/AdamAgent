"""单次本地视觉问答命令；显式选会话、图版本和模型，不调用默认付费 API。"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from adam.bootstrap import open_image_questions
from adam.config.consumption import DEFAULT_CONSUMPTION, load_consumption_config
from adam.config.vision import LocalVisionConfig
from adam.contracts.errors import AgentError, RequestInterrupted


def main() -> int:
    """输出已提交回答和输入图引用；失败返回非零，Ctrl+C/取消不自动重发。"""
    parser = argparse.ArgumentParser(
        description="Ask a local llama.cpp vision model about a saved image"
    )
    parser.add_argument("--sessions-dir", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--media-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8087/v1")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--consumption-config", type=Path)
    args = parser.parse_args()
    try:
        local = LocalVisionConfig(
            args.model, args.endpoint, args.timeout, stream=not args.no_stream
        )
        consumption = (
            load_consumption_config(args.consumption_config)
            if args.consumption_config
            else DEFAULT_CONSUMPTION
        )
        with open_image_questions(
            args.sessions_dir, args.session, local, consumption=consumption
        ) as questions:
            answer = questions.ask_saved(args.question, args.media_id, args.version)
        print(json.dumps({"ok": True, **asdict(answer)}, ensure_ascii=False))
        return 0
    except AgentError as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": error.code.value,
                    "error": str(error),
                    "partial_reply": error.partial_reply,
                },
                ensure_ascii=False,
            )
        )
        return 1
    except (RequestInterrupted, KeyboardInterrupt):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
