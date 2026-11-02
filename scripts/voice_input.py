"""C05.1本地按键录音→离线转写→用户确认；此入口不发送聊天请求。"""

import argparse
from pathlib import Path

from adam.application.subagents.unit_budget import UnitBudget
from adam.audio.adapters.microphone import Microphone
from adam.audio.adapters.whisper import WhisperASR
from adam.audio.input import SpeechInput
from adam.contracts.cancellation import CancellationToken
from adam.contracts.consumption import Quantities


def main() -> None:
    """Enter开始/结束，显示原始转写并允许编辑；Ctrl+C释放设备且不提交文字。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--device", type=int)
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    args = parser.parse_args()
    microphone = Microphone(args.device)
    service = SpeechInput(
        args.records,
        WhisperASR(args.python, args.model, language=args.language),
        UnitBudget(Quantities(output_tokens=1024, audio_ms=30000, calls=1)),
    )
    try:
        input("按Enter开始录音（最多30秒），Ctrl+C取消：")
        microphone.start()
        input("正在录音，按Enter结束：")
        recording = microphone.finish()
        draft = service.transcribe(recording, cancel=CancellationToken())
        print("原始转写：" + draft.transcript)
        edited = input("Enter确认原文，或输入修正文；/cancel放弃：")
        if edited == "/cancel":
            service.discard(draft.capture_id)
            print("已放弃，未发送聊天请求。")
        else:
            confirmed = service.confirm(draft.capture_id, edited or draft.transcript)
            print("已确认草稿 " + confirmed.capture_id + "：" + str(confirmed.confirmed_text))
            print("当前只保存草稿，未发送聊天请求。")
    finally:
        microphone.cancel()


if __name__ == "__main__":
    main()
