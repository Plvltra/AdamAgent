"""将已确认语音草稿提交原应用一次；重复运行只查询原轮次，不重新请求模型。"""

import argparse
import json
from functools import partial
from pathlib import Path

import httpx

from adam.application.conversation import Conversation
from adam.application.voice import VoiceConversation
from adam.audio.input import load_confirmed
from adam.bootstrap import build_application
from adam.cli.main import confirm_command, confirm_edit
from adam.config.loader import load_config
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
            print(json.dumps(voice.send(args.capture_id), ensure_ascii=False, indent=2))
        finally:
            app.close()


if __name__ == "__main__":
    main()
