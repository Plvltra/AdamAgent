"""Terminal interaction and in-memory conversation history."""

import argparse
from collections.abc import Callable
from pathlib import Path

import httpx

from adam.config import ConfigError, load_config
from adam.llm import LLMClient, LLMError


def chat(
    client: LLMClient,
    system_prompt: str,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> None:
    def initial_history() -> list[dict[str, str]]:
        return [{"role": "system", "content": system_prompt}] if system_prompt else []

    # Runtime selection is separate from the immutable startup configuration.
    current_model = client.config.model
    models: dict[str, str | None] | None = None

    def show_model() -> None:
        name = models.get(current_model) if models is not None else None
        write(f"当前模型：{name or '具体版本未知'}")
        if models is None or current_model not in models:
            write("模型 ID 尚未验证；将按此 ID 发起请求。")

    history = initial_history()
    write("Adam — /model 查看或切换模型，/new 新对话，/exit 退出；历史仅在本次运行内保留。")
    try:
        try:
            models = client.list_models()
        except LLMError:
            write("模型目录暂时无法获取，仍可继续聊天。")

        show_model()

        while True:
            text = read("你> ").strip()
            if not text:
                continue

            if text == "/exit":
                break

            if text == "/new":
                history = initial_history()
                write("已清空聊天历史，保留系统提示词。")
                continue

            parts = text.split(maxsplit=1)
            if parts[0] == "/model":
                if len(parts) == 1:
                    show_model()
                    if models is not None:
                        write("可用模型：")
                        for name in models.values():
                            write(f"  {name or '具体版本未知'}")
                else:
                    requested_model = parts[1].strip()
                    if models is not None and requested_model not in models:
                        write("模型 ID 不在服务端目录中，未切换；请使用服务商支持的 API 模型 ID。")
                        continue

                    current_model = requested_model
                    write("已切换后续请求的模型；保留历史，仅本次运行有效。")
                    show_model()
                continue

            user_message = {"role": "user", "content": text}
            try:
                reply = client.complete([*history, user_message], model=current_model)
            except LLMError as error:
                write(f"错误：{error}")
                continue

            # Commit a complete turn only after the request succeeds.
            history.extend([user_message, {"role": "assistant", "content": reply}])
            write(f"Adam> {reply}")
    except (EOFError, KeyboardInterrupt):
        pass

    write("再见。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Adam 内存会话终端聊天")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"配置错误：{error}")
        return 2

    with httpx.Client(follow_redirects=False) as http:
        chat(LLMClient(config, http), config.system_prompt)

    return 0
