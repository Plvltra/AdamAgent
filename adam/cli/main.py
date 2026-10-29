"""终端输入、命令和展示；通过应用接口操作，不创建或访问底层客户端。"""

import argparse
import json
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

from colorama import just_fix_windows_console

from adam.application.facade import AgentApplication
from adam.bootstrap import open_application
from adam.cli.memory_commands import handle_memory_command
from adam.cli.reply_output import ReplyOutput, write_fragment
from adam.cli.terminal import role_prefix, show_context, show_execution_report, show_failure
from adam.config.loader import adam_home
from adam.contracts.errors import AgentError
from adam.contracts.models import CommandRequest, EditPlan, valid_session_id


def show_session(application: AgentApplication, write: Callable[[str], None]) -> None:
    session = application.session
    if session is not None:
        write(f"当前会话：{session.session_id} — {session.title}")


class CommandResult(Enum):
    NOT_COMMAND = auto()
    HANDLED = auto()
    EXIT = auto()


def show_model(
    application: AgentApplication,
    write: Callable[[str], None],
) -> None:
    models = application.models
    name = models.get(application.model) if models is not None else None
    write(f"当前模型：{name or '具体版本未知'}")
    if models is None or application.model not in models:
        write("模型 ID 尚未验证；将按此 ID 发起请求。")


def show_usage(application: AgentApplication, write: Callable[[str], None]) -> None:
    """按需显示当前会话的已知用量与额度；不创建会话或发起模型请求。"""
    session = application.session
    if session is None:
        write("尚未创建会话，用量：0 token。")
        return
    usage = application.usage()
    write(f"用量> 会话累计 {usage.tokens}/{usage.limit} token")
    if usage.unknown:
        write("以上仅为已知用量；统计不完整，当前会话不能继续请求模型。")


def handle_command(
    text: str,
    application: AgentApplication,
    write: Callable[[str], None],
) -> CommandResult:
    """统一处理终端命令；普通文本交给聊天流程，未知命令不发送给模型。"""
    text = text.strip()
    if not text.startswith("/"):
        return CommandResult.NOT_COMMAND
    parts = text.split(maxsplit=1)
    command = parts[0]
    if command == "/exit" and len(parts) == 1:
        return CommandResult.EXIT
    if command == "/new":
        application.new(parts[1] if len(parts) == 2 else "新对话")
        show_session(application, write)
        write("已新建会话，原会话仍保留。")
    elif command == "/memory":
        handle_memory_command(parts[1] if len(parts) == 2 else "", application, write)
    elif command == "/usage":
        if len(parts) != 1:
            write("用法：/usage")
        else:
            show_usage(application, write)
    elif command == "/compact":
        if len(parts) != 1:
            write("用法：/compact")
        else:
            application.compact()
    elif command == "/context":
        option = parts[1] if len(parts) == 2 else ""
        mode, delimiter, query = option.partition("--query ")
        mode = mode.strip()
        if mode not in {"", "full"} or (delimiter and not query.strip()):
            write("用法：/context [full]")
            write("也可 /context [full] --query 待预览的问题")
        else:
            preview = (
                application.preview_context(query) if delimiter else application.preview_context()
            )
            show_context(preview, write, full=mode == "full")
    elif command == "/sessions":
        sessions = application.list_sessions()
        if not sessions:
            write("暂无保存的会话。")
        for session in sessions:
            marker = "*" if session == application.session else " "
            write(f"{marker} {session.session_id} — {session.title}")
    elif command == "/resume":
        if len(parts) != 2 or not valid_session_id(parts[1]):
            write("用法：/resume <8 位小写十六进制会话ID>")
            return CommandResult.HANDLED
        session_id = parts[1]
        application.resume(session_id)
        show_session(application, write)
        for message in application.history():
            write(role_prefix(message.role) + message.content)
    elif command == "/model":
        models = application.models
        if len(parts) == 1:
            show_model(application, write)
            if models is not None:
                write("可用模型：")
                for name in models.values():
                    write(f"  {name or '具体版本未知'}")
        else:
            requested_model = parts[1]
            application.select_model(requested_model)
            write("已切换后续请求的模型；保留历史，仅本次运行有效。")
            show_model(application, write)
    else:
        write(
            "未知命令，请使用 /new、/sessions、/resume、/model、/usage、/context、/compact、/memory 或 /exit。"
        )
    return CommandResult.HANDLED


def confirm_command(
    command: CommandRequest, read: Callable[[str], str], write: Callable[[str], None]
) -> bool:
    """展示本次完整执行计划；仅明确输入 y 批准，其他输入或 EOF 拒绝；Ctrl+C 取消当前轮次。"""
    write(f"工作目录> {command.cwd}")
    write("命令参数> " + json.dumps(command.argv, ensure_ascii=False))
    write(f"超时> {command.timeout_seconds} 秒；执行能力由当前账户权限决定。")
    try:
        return read("允许本次执行？[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def confirm_edit(plan: EditPlan, read: Callable[[str], str], write: Callable[[str], None]) -> bool:
    """展示已规划的完整 diff；只批准当前计划，非 y 输入拒绝，Ctrl+C 取消当前轮次。"""
    write(f"修改文件> {plan.path}")
    write(plan.diff)
    try:
        return read("允许本次文件修改？[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def show_continuation(application: AgentApplication, write: Callable[[str], None]) -> None:
    """失败后展示应用给出的继续方式；日志不可读时不猜测会话仍可执行。"""
    try:
        status = application.continuation_status()
    except AgentError:
        write("继续方式> 无法读取会话状态；请先检查日志和文件权限，未自动重试。")
        return
    write("继续方式> " + status.message)


def show_turn_report(application: AgentApplication, write: Callable[[str], None]) -> None:
    """通过应用取得执行事实；日志故障不掩盖原始失败，终端写入异常仍传播。"""
    try:
        report = application.execution_report()
    except AgentError:
        write("执行记录> 日志无法读取，实际结果无法确认；请检查会话文件，未自动重做。")
        return
    show_execution_report(report, write)


def send_message(
    application: AgentApplication,
    text: str,
    write: Callable[[str], None],
    fragment: Callable[[str], None] | None,
) -> None:
    """增量展示本轮回答，结束后附执行事实；已知失败说明继续方式，中断交外层处理。"""
    output = ReplyOutput(write, fragment or (write_fragment if write is print else write))
    try:
        reply = application.send(text, on_output=output.receive)
    except AgentError as error:
        output.incomplete()
        show_failure(error, write)
        show_turn_report(application, write)
        show_continuation(application, write)
    except (KeyboardInterrupt, EOFError):
        output.incomplete()
        show_turn_report(application, write)
        raise
    except BaseException:
        output.incomplete()
        raise
    else:
        output.complete(reply)
        show_turn_report(application, write)


def chat(
    application: AgentApplication,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    resume_id: str | None = None,
    fragment: Callable[[str], None] | None = None,
) -> None:
    """运行终端交互并展示 AgentError；资源由调用方关闭，意外错误继续传播。"""
    if resume_id is not None:
        application.resume(resume_id)

    write(
        "Adam — /new [名称]、/sessions、/resume <ID>、/model、/usage、/context [full]、/compact、/memory、/exit；成功轮次自动保存。"
    )
    show_session(application, write)
    try:
        if not application.refresh_models():
            write("模型目录暂时无法获取，仍可继续聊天。")
        show_model(application, write)
        while True:
            text = read(role_prefix("user")).strip()
            if not text:
                continue
            try:
                result = handle_command(text, application, write)
                if result is CommandResult.EXIT:
                    break
                if result is CommandResult.HANDLED:
                    continue
                send_message(application, text, write, fragment)
            except KeyboardInterrupt:
                write("已取消本轮，当前会话保留；已发生的工具操作不会回滚。")
                show_continuation(application, write)
            except AgentError as error:
                show_failure(error, write)
                show_continuation(application, write)
    except (EOFError, KeyboardInterrupt):
        pass
    write("再见。")


def main() -> int:
    """组装并启动应用；正常退出返回 0，传播到启动边界的已知失败返回 2。"""
    just_fix_windows_console()
    parser = argparse.ArgumentParser(description="Adam 持久会话终端聊天")
    parser.add_argument("--config", type=Path, default=adam_home() / "config.toml")
    parser.add_argument("--llm-config", type=Path, help="模型配置路径，默认主配置旁的 llm.toml")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="工具工作目录")
    parser.add_argument("--resume", help="启动时恢复指定的 8 位会话 ID")
    args = parser.parse_args()
    if args.resume is not None and not valid_session_id(args.resume):
        parser.error("--resume 必须是 8 位小写十六进制会话 ID")
    try:
        with open_application(
            args.config,
            args.llm_config,
            args.workspace,
            confirm_command=lambda command: confirm_command(command, input, print),
            confirm_edit=lambda plan: confirm_edit(plan, input, print),
            on_tool=lambda name: print(f"工具> {name}"),
            on_notice=print,
        ) as application:
            print(f"会话目录：{application.sessions_dir}")
            chat(application, resume_id=args.resume)
    except AgentError as error:
        show_failure(error)
        return 2
    return 0
