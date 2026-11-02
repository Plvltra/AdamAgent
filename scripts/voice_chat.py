"""将已确认语音草稿提交原应用一次；重复运行只查询原轮次，不重新请求模型。"""

import argparse
import json
from functools import partial
from pathlib import Path

import httpx

from adam.application.conversation import Conversation
from adam.application.subagents.unit_budget import UnitBudget
from adam.application.voice import VoiceConversation
from adam.audio.adapters.windows_speech import WindowsSpeech
from adam.audio.input import load_confirmed
from adam.audio.output import SpeechOutput
from adam.bootstrap import build_application
from adam.cli.main import confirm_command, confirm_edit
from adam.config.loader import load_config
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import Quantities
from adam.llm.client import LLMClient
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore


def main() -> None:
    """沿用配置和原命令/编辑批准；恢复须显式提供所属session，不暗中跨会话读取。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--llm-config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--session")
    parser.add_argument("--speak", action="store_true", help="成功回答后朗读；Ctrl+C停止")
    args = parser.parse_args()
    config = load_config(args.config, args.llm_config)
    conversations: list[Conversation] = []
    with SessionStore(config.sessions_dir) as store, httpx.Client() as http:
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            args.workspace,
            observe_conversation=conversations.append,
            confirm_command=lambda request: confirm_command(request, input, print),
            confirm_edit=lambda plan: confirm_edit(plan, input, print),
        )
        try:
            if args.session:
                app.resume(args.session)
            voice = VoiceConversation(
                app,
                conversations[0],
                store,
                SessionMedia(store, MediaStore(store.root / "_media", max_bytes=16777216)),
                partial(load_confirmed, args.drafts),
            )
            result = voice.send(args.capture_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.speak and result["state"] == "succeeded":
                speech = SpeechOutput(
                    store.root / "_media" / "speech_output",
                    WindowsSpeech(),
                    UnitBudget(Quantities(audio_ms=60000, calls=1)),
                )
                try:
                    print(
                        json.dumps(
                            speech.speak(
                                result["session_id"],
                                result["turn_id"],
                                result["reply"],
                                cancel=CancellationToken(),
                            ),
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                except (Exception, KeyboardInterrupt) as error:
                    print("模型回答已成功；朗读未完成：" + type(error).__name__)
        finally:
            app.close()


if __name__ == "__main__":
    main()
