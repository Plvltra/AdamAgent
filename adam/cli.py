"""终端交互；会话流程和会话存储由各自模块负责。"""

import argparse
import json
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

import httpx
from colorama import just_fix_windows_console

from adam.config import AgentConfig, adam_home, load_config
from adam.conversation import Conversation
from adam.errors import AgentError
from adam.llm import LLMClient
from adam.storage import SessionStore, valid_session_id
from adam.terminal import role_prefix, show_failure
from adam.tool_calls import PermissionConfig, ToolLimits, ToolRegistry
from adam.tool_calls.command_tools import CommandRequest


def show_session(conversation: Conversation, write: Callable[[str], None]) -> None:
    session = conversation.session
    if session is not None:
        write(f"当前会话：{session.session_id} — {session.title}")


class CommandResult(Enum):
    NOT_COMMAND = auto()
    HANDLED = auto()
    EXIT = auto()


def show_model(
    conversation: Conversation,
    models: dict[str, str | None] | None,
    write: Callable[[str], None],
) -> None:
    name = models.get(conversation.model) if models is not None else None
    write(f"当前模型：{name or '具体版本未知'}")
    if models is None or conversation.model not in models:
        write("模型 ID 尚未验证；将按此 ID 发起请求。")


def show_usage(conversation: Conversation, write: Callable[[str], None]) -> None:
    """按需显示当前会话的已知用量与额度；不创建会话或发起模型请求。"""
    session = conversation.session
    if session is None:
        write("尚未创建会话，用量：0 token。")
        return
    total, unknown = conversation.store.usage(session.session_id)
    write(f"用量> 会话累计 {total}/{conversation.limits.max_session_tokens} token")
    if unknown:
        write("以上仅为已知用量；统计不完整，当前会话不能继续请求模型。")


def handle_command(
    text: str,
    conversation: Conversation,
    models: dict[str, str | None] | None,
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
        conversation.new(parts[1] if len(parts) == 2 else "新对话")
        show_session(conversation, write)
        write("已新建会话，原会话仍保留。")
    elif command == "/usage":
        if len(parts) != 1:
            write("用法：/usage")
        else:
            show_usage(conversation, write)
    elif command == "/sessions":
        sessions = conversation.store.list_sessions()
        if not sessions:
            write("暂无保存的会话。")
        for session in sessions:
            marker = "*" if session == conversation.session else " "
            write(f"{marker} {session.session_id} — {session.title}")
    elif command == "/resume":
        if len(parts) != 2 or not valid_session_id(parts[1]):
            write("用法：/resume <8 位小写十六进制会话ID>")
            return CommandResult.HANDLED
        session_id = parts[1]
        conversation.resume(session_id)
        show_session(conversation, write)
        for message in conversation.store.load_messages(session_id):
            write(role_prefix(message.role) + message.content)
    elif command == "/model":
        if len(parts) == 1:
            show_model(conversation, models, write)
            if models is not None:
                write("可用模型：")
                for name in models.values():
                    write(f"  {name or '具体版本未知'}")
        else:
            requested_model = parts[1]
            if models is not None and requested_model not in models:
                write("模型 ID 不在服务端目录中，未切换；请使用服务商支持的 API 模型 ID。")
                return CommandResult.HANDLED
            conversation.model = requested_model
            write("已切换后续请求的模型；保留历史，仅本次运行有效。")
            show_model(conversation, models, write)
    else:
        write("未知命令，请使用 /new、/sessions、/resume、/model、/usage 或 /exit。")
    return CommandResult.HANDLED


def confirm_command(
    command: CommandRequest, read: Callable[[str], str], write: Callable[[str], None]
) -> bool:
    """展示本次完整执行计划；仅明确输入 y 批准，其他输入、EOF 或取消均拒绝。"""
    write(f"工作目录> {command.cwd}")
    write("命令参数> " + json.dumps(command.argv, ensure_ascii=False))
    write(f"超时> {command.timeout_seconds} 秒；执行能力由当前账户权限决定。")
    try:
        return read("允许本次执行？[y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def chat(
    client: LLMClient,
    system_prompt: str,
    store: SessionStore,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    resume_id: str | None = None,
    workspace: Path | None = None,
    permissions: PermissionConfig | None = None,
    tool_limits: ToolLimits | None = None,
    agent_config: AgentConfig | None = None,
) -> None:
    """运行终端交互；已知失败按 AgentError 展示，意外错误传播并释放会话锁。"""
    try:
        conversation = Conversation(
            store,
            client,
            system_prompt,
            ToolRegistry(
                workspace or Path.cwd(),
                permissions,
                tool_limits,
                confirm_command=lambda command: confirm_command(command, read, write),
            ),
            agent_config,
            write,
        )
        if resume_id is not None:
            conversation.resume(resume_id)
        models: dict[str, str | None] | None = None

        write(
            "Adam — /new [名称]、/sessions、/resume <ID>、/model、/usage、/exit；成功轮次自动保存。"
        )
        show_session(conversation, write)
        try:
            models = client.list_models()
        except AgentError:
            write("模型目录暂时无法获取，仍可继续聊天。")
        show_model(conversation, models, write)
        while True:
            text = read(role_prefix("user")).strip()
            if not text:
                continue
            try:
                result = handle_command(text, conversation, models, write)
                if result is CommandResult.EXIT:
                    break
                if result is CommandResult.HANDLED:
                    continue
                reply = conversation.send(text)
                write(role_prefix("assistant") + reply)
            except AgentError as error:
                show_failure(error, write)
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        store.close()
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
        config = load_config(args.config, args.llm_config)
    except AgentError as error:
        show_failure(error)
        return 2
    try:
        with (
            SessionStore(config.sessions_dir) as store,
            httpx.Client(follow_redirects=False) as http,
        ):
            print(f"会话目录：{config.sessions_dir}")
            chat(
                LLMClient(config.llm, http),
                config.system_prompt,
                store,
                resume_id=args.resume,
                workspace=args.workspace,
                permissions=config.permissions,
                tool_limits=config.tool_limits,
                agent_config=config.agent,
            )
    except AgentError as error:
        show_failure(error)
        return 2
    return 0
