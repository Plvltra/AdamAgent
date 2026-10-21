"""终端交互；会话流程和数据库操作由各自模块负责。"""

import argparse
import sqlite3
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

import httpx

from adam.config import ConfigError, load_config
from adam.conversation import Conversation, TurnSaveError
from adam.llm import LLMClient, LLMError
from adam.storage import SessionNotFoundError, SessionStore


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
    elif command == "/sessions":
        sessions = conversation.store.list_sessions()
        if not sessions:
            write("暂无保存的会话。")
        for session in sessions:
            marker = "*" if session == conversation.session else " "
            write(f"{marker} {session.session_id} — {session.title}")
    elif command == "/resume":
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            write("用法：/resume <正整数会话ID>")
            return CommandResult.HANDLED
        if len(parts[1]) > 19 or not 0 < int(parts[1]) <= 2**63 - 1:
            write("会话 ID 超出有效范围。")
            return CommandResult.HANDLED
        session_id = int(parts[1])
        conversation.resume(session_id)
        show_session(conversation, write)
        for message in conversation.store.load_messages(session_id):
            write(f"{message.role}> {message.content}")
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
        write("未知命令，请使用 /new、/sessions、/resume、/model 或 /exit。")
    return CommandResult.HANDLED


def chat(
    client: LLMClient,
    system_prompt: str,
    store: SessionStore,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    resume_id: int | None = None,
) -> None:
    conversation = Conversation(store, client, system_prompt)
    if resume_id is not None:
        conversation.resume(resume_id)
    models: dict[str, str | None] | None = None

    write("Adam — /new [名称]、/sessions、/resume <ID>、/model、/exit；成功轮次自动保存。")
    show_session(conversation, write)
    try:
        try:
            models = client.list_models()
        except LLMError:
            write("模型目录暂时无法获取，仍可继续聊天。")
        show_model(conversation, models, write)
        while True:
            text = read("你> ").strip()
            if not text:
                continue
            try:
                result = handle_command(text, conversation, models, write)
                if result is CommandResult.EXIT:
                    break
                if result is CommandResult.HANDLED:
                    continue
                reply = conversation.send(text)
                write(f"Adam> {reply}")
            except TurnSaveError as error:
                write(f"Adam（未保存）> {error.reply}")
                write(str(error))
            except LLMError as error:
                write(f"模型请求错误：{error}")
            except SessionNotFoundError as error:
                write(str(error))
            except sqlite3.Error:
                write("数据库操作失败，请检查文件权限或是否被占用；未自动重试。")
    except (EOFError, KeyboardInterrupt):
        pass
    write("再见。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Adam 持久会话终端聊天")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--llm-config", type=Path, help="模型配置路径，默认主配置旁的 llm.toml")
    parser.add_argument(
        "--db", type=Path, help="覆盖 storage.db_path；相对路径以当前工作目录为基准"
    )
    parser.add_argument("--resume", type=int, help="启动时恢复指定会话 ID")
    args = parser.parse_args()
    if args.resume is not None and not 0 < args.resume <= 2**63 - 1:
        parser.error("--resume 必须是有效的正整数会话 ID")
    try:
        config = load_config(args.config, args.llm_config)
    except ConfigError as error:
        print(f"配置错误：{error}")
        return 2
    db_path = args.db if args.db is not None else config.db_path
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with SessionStore(db_path) as store, httpx.Client(follow_redirects=False) as http:
            print(f"会话数据库：{db_path.resolve()}")
            chat(LLMClient(config.llm, http), config.system_prompt, store, resume_id=args.resume)
    except SessionNotFoundError as error:
        print(str(error))
        return 2
    except (OSError, sqlite3.Error):
        print("数据库无法创建、打开或读取，请检查路径、权限和数据库文件。")
        return 2
    return 0
