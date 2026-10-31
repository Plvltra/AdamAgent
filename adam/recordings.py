"""python -m adam.recordings：显式录制一轮或离线重放；重放分支不读取模型配置。"""

import argparse
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from adam.bootstrap import build_application
from adam.cli.main import confirm_command, confirm_edit
from adam.cli.reply_output import ReplyOutput, write_fragment
from adam.config.loader import adam_home, load_config
from adam.config.settings import AppConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import AgentError
from adam.contracts.execution import ExecutionMode
from adam.contracts.ports import ModelClient
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.recording.capture import RecordingModel, TurnRecorder
from adam.recording.journal import RecordingWriter, read_recording
from adam.recording.runtime import CaptureRuntime, replay_turn
from adam.recording.snapshot import capture_seed
from adam.session.storage import SessionStore


def record_turn(
    config: AppConfig,
    workspace: Path,
    output: Path,
    text: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    mode: ExecutionMode = ExecutionMode.ACT,
    skill_id: str | None = None,
) -> str:
    """显式执行一轮真实任务并录制；沿用原审批、子任务与资源清理，不自动重试录制失败。"""

    @contextmanager
    def child_client(cancel: CancellationToken) -> Iterator[ModelClient]:
        with httpx.Client(transport=InterruptibleTransport(cancel), follow_redirects=False) as http:
            yield LLMClient(config.llm, http)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=InterruptibleTransport(), follow_redirects=False) as http,
    ):
        sid = session_id or store.create_session(text[:40], config.system_prompt)
        store.select_session(sid)
        seed = capture_seed(config, store, sid, text, workspace, model=model, mode=mode)
        with RecordingWriter(output, seed) as writer:
            recorder = TurnRecorder(writer, lambda: store.load_events(sid))
            runtime = CaptureRuntime(recorder)
            app = build_application(
                config,
                store,
                RecordingModel(LLMClient(config.llm, http), recorder),
                workspace,
                confirm_command=lambda command: confirm_command(command, input, print),
                confirm_edit=lambda plan: confirm_edit(plan, input, print),
                on_tool=lambda name: print("工具> " + name),
                on_notice=print,
                child_client_factory=child_client,
                decorate_tools=runtime.tools,
                observe_conversation=runtime.bind,
            )
            display = ReplyOutput(print, write_fragment)
            try:
                app.resume(sid)
                app.select_mode(mode.value)
                if model is not None:
                    app.select_model(model)

                reply = recorder.run(
                    lambda: app.send(text, on_output=display.receive, skill_id=skill_id)
                )
                display.complete(reply)
            except BaseException as error:
                try:
                    display.incomplete()
                except Exception:
                    error.add_note("终端未完成提示失败；原始错误保留。")
                try:
                    app.close()
                except Exception:
                    error.add_note("应用清理失败；原始录制或业务错误保留。")
                raise
            else:
                app.close()
                return reply


def main(argv: list[str] | None = None) -> int:
    """独立开发入口；重放只需要录制及全新输出目录，差异/缺失分别使用非零退出码。"""
    parser = argparse.ArgumentParser(description="显式单轮录制与离线重放")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("record", help="执行真实一轮并录制，使用原权限与审批")
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--text", required=True)
    capture.add_argument("--config", type=Path, default=adam_home() / "config.toml")
    capture.add_argument("--llm-config", type=Path)
    capture.add_argument("--workspace", type=Path, default=Path.cwd())
    capture.add_argument("--session")
    capture.add_argument("--model")
    capture.add_argument("--mode", choices=["act", "plan"], default="act")
    capture.add_argument("--skill")
    replay = commands.add_parser("replay", help="不调用真实模型和工具，在新目录比较流程")
    replay.add_argument("recording", type=Path)
    replay.add_argument("--output", required=True, type=Path)
    inspect = commands.add_parser("inspect", help="只读显示录制完整性及调用数量")
    inspect.add_argument("recording", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            report = replay_turn(args.recording, args.output)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return {"matched": 0, "different": 1, "incomplete": 2}[report["status"]]
        if args.command == "inspect":
            recording = read_recording(args.recording)
            print(
                json.dumps(
                    {
                        "complete": recording.complete,
                        "torn_tail": recording.torn_tail,
                        "events": len(recording.events),
                        "calls": sum(item["type"] == "call_started" for item in recording.events),
                        "session_id": recording.seed["session_id"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        config = load_config(args.config, args.llm_config)
        record_turn(
            config,
            args.workspace,
            args.output,
            args.text,
            session_id=args.session,
            model=args.model,
            mode=ExecutionMode(args.mode),
            skill_id=args.skill,
        )
        print(f"录制已保存：{args.output.resolve()}")
        return 0
    except (AgentError, OSError, ValueError) as error:
        print(f"录制/重放未完成：{error}")
        return 2
    except KeyboardInterrupt:
        print("已中断；保留可读取的录制或独立诊断前缀。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
