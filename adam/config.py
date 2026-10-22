"""Read and validate the local TOML configuration."""

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 120


def load_llm_config(path: Path) -> LLMConfig:
    try:
        # utf-8-sig also accepts files saved with a Windows UTF-8 BOM.
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"无法读取模型配置 {path}，请检查文件存在且为有效 UTF-8 TOML。") from None

    if data.keys() - {"llm"}:
        raise ConfigError("模型配置仅支持 [llm]，请对照 llm.example.toml。")

    section = data.get("llm")
    if not isinstance(section, dict):
        raise ConfigError("配置缺少 [llm] 表。")

    allowed = {"base_url", "model", "api_key", "api_key_env", "timeout_seconds"}
    if section.keys() - allowed:
        raise ConfigError("[llm] 中存在不支持的配置项，请对照 llm.example.toml。")

    def required_text(name: str) -> str:
        value = section.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"llm.{name} 必须是非空字符串。")
        return value.strip()

    base_url = required_text("base_url").rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = (
            url.scheme in {"http", "https"}
            and url.hostname
            and not (url.username or url.password or url.query or url.fragment)
        )
        # Accessing port also validates its numeric value and range.
        _ = url.port
    except ValueError:
        valid = False

    if not valid:
        raise ConfigError("base_url 必须是无凭据、查询参数和片段的 HTTP(S) 地址。")

    model = required_text("model")

    if ("api_key" in section) == ("api_key_env" in section):
        raise ConfigError("api_key 与 api_key_env 必须且只能配置一个。")

    if "api_key" in section:
        key = required_text("api_key")
    else:
        key = os.environ.get(required_text("api_key_env"), "").strip()
        if not key:
            raise ConfigError("api_key_env 指定的环境变量未设置或为空。")

    if not key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise ConfigError("API 密钥必须是无空白的可打印 ASCII 字符串。")

    timeout = section.get("timeout_seconds", 120)
    # bool is an int subclass, but is not a valid timeout.
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("timeout_seconds 必须是有限的正数。")

    return LLMConfig(
        base_url=base_url,
        model=model,
        api_key=key,
        timeout_seconds=float(timeout),
    )


@dataclass(frozen=True)
class AppConfig:
    llm: LLMConfig
    db_path: Path
    system_prompt: str


def load_config(path: Path, llm_path: Path | None = None) -> AppConfig:
    """加载主配置；相对存储路径和默认模型配置以主配置目录为基准。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError(f"无法读取主配置 {path}，请检查文件存在且为有效 UTF-8 TOML。") from None

    if data.keys() - {"storage", "chat"}:
        raise ConfigError("主配置仅支持 [storage] 和 [chat]，请对照 config.example.toml。")

    storage = data.get("storage", {})
    chat = data.get("chat", {})

    if not isinstance(storage, dict) or storage.keys() - {"db_path"}:
        raise ConfigError("[storage] 仅支持 db_path。")

    if not isinstance(chat, dict) or chat.keys() - {"system_prompt"}:
        raise ConfigError("[chat] 仅支持 system_prompt。")

    db_value = storage.get("db_path", "data/adam.db")
    if not isinstance(db_value, str) or not db_value.strip() or "\0" in db_value:
        raise ConfigError("storage.db_path 必须是非空且不含空字符的路径字符串。")

    prompt = chat.get("system_prompt", "你是 Adam，一个友好的助手。")
    if not isinstance(prompt, str):
        raise ConfigError("chat.system_prompt 必须是字符串。")

    directory = path.resolve().parent
    db_path = Path(db_value)
    if not db_path.is_absolute():
        db_path = directory / db_path

    model_config_path = llm_path if llm_path is not None else directory / "llm.toml"
    llm = load_llm_config(model_config_path)

    return AppConfig(
        llm=llm,
        db_path=db_path,
        system_prompt=prompt,
    )
