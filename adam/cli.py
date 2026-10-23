"""终端交互；会话流程和数据库操作由各自模块负责。"""

import argparse
from collections.abc import Callable
from enum import Enum, auto
from pathlib import Path

import httpx

from adam.config import ConfigError, adam_home, load_config
from adam.conversation import Conversation, TurnSaveError
from adam.llm import LLMClient, LLMError
from adam.storage import SessionNotFoundError, SessionStore, StorageError, valid_session_id


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
        if len(parts) != 2 or not valid_session_id(parts[1]):
            write("用法：/resume <8 位小写十六进制会话ID>")
            return CommandResult.HANDLED
        session_id = parts[1]
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
    resume_id: str | None = None,
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
            except StorageError as error:
                write(str(error))
            except OSError:
                write("会话文件操作失败，请检查路径和权限；未自动重试。")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        store.close()
    write("再见。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Adam 持久会话终端聊天")
    parser.add_argument("--config", type=Path, default=adam_home() / "config.toml")
    parser.add_argument("--llm-config", type=Path, help="模型配置路径，默认主配置旁的 llm.toml")
    parser.add_argument("--resume", help="启动时恢复指定的 8 位会话 ID")
    args = parser.parse_args()
    if args.resume is not None and not valid_session_id(args.resume):
        parser.error("--resume 必须是 8 位小写十六进制会话 ID")
    try:
        config = load_config(args.config, args.llm_config)
    except ConfigError as error:
        print(f"配置错误：{error}")
        return 2
    try:
        with (
            SessionStore(config.sessions_dir) as store,
            httpx.Client(follow_redirects=False) as http,
        ):
            print(f"会话目录：{config.sessions_dir}")
            chat(LLMClient(config.llm, http), config.system_prompt, store, resume_id=args.resume)
    except SessionNotFoundError as error:
        print(str(error))
        return 2
    except StorageError as error:
        print(str(error))
        return 2
    except OSError:
        print("会话目录无法创建、打开或读取，请检查路径和权限。")
        return 2
    return 0
