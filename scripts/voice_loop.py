"""按键语音连续对话；本地ASR/TTS，原模型线程及B27安全点，不自动批准工具。"""

import argparse
from functools import partial
from pathlib import Path

import httpx

from adam.application.conversation import Conversation
from adam.application.subagents.unit_budget import UnitBudget
from adam.application.voice import VoiceConversation
from adam.application.voice_loop import VoiceLoop
from adam.audio.adapters.microphone import Microphone
from adam.audio.adapters.whisper import WhisperASR
from adam.audio.adapters.windows_speech import WindowsSpeech
from adam.audio.input import SpeechInput, load_confirmed
from adam.audio.output import SpeechOutput
from adam.audio.playback import SpeechPlayback
from adam.bootstrap import build_application
from adam.config.loader import load_config
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import Quantities
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore


def main() -> None:
    """显式本地模型/设备/草稿配置；默认plan模式，唯一输入线程不兼作工具批准。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--llm-config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--device", type=int)
    parser.add_argument("--session")
    args = parser.parse_args()
    config = load_config(args.config, args.llm_config)
    cancel = CancellationToken()
    conversations: list[Conversation] = []
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=InterruptibleTransport(cancel), follow_redirects=False) as http,
    ):
        app = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            args.workspace,
            observe_conversation=conversations.append,
            live_input=True,
            cancel=cancel,
        )
        try:
            if args.session:
                app.resume(args.session)
            media = SessionMedia(store, MediaStore(store.root / "_media", max_bytes=16777216))
            voice = VoiceConversation(
                app, conversations[0], store, media, partial(load_confirmed, args.records)
            )
            speech = SpeechInput(
                args.records,
                WhisperASR(args.python, args.model),
                UnitBudget(Quantities(output_tokens=10240, audio_ms=300000, calls=10)),
            )
            playback = SpeechPlayback(
                SpeechOutput(
                    store.root / "_media/speech_output",
                    WindowsSpeech(),
                    UnitBudget(Quantities(audio_ms=600000, calls=10)),
                )
            )
            VoiceLoop(voice, speech, Microphone(args.device), playback, cancel, input, print).run()
        finally:
            app.close()


if __name__ == "__main__":
    main()
