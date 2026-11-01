"""图片能力的显式配置与默认关闭边界；不打开图片或请求模型。"""

import subprocess
import sys
from pathlib import Path

import pytest

from adam.config.loader import load_config
from adam.config.settings import VisionConfig
from adam.contracts.errors import ConfigError


def config_file(root, section):
    """写入临时主/模型配置，不读取真实用户配置或凭据。"""
    (root / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="not-real"', encoding="utf-8"
    )
    path = root / "config.toml"
    path.write_text(section, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "section,enabled",
    [
        ("", False),
        ("[vision]\nenabled=true\nmax_bytes=100\nmax_pixels=50\nmax_dimension=10", True),
    ],
)
def test_optional_vision_config_has_no_implicit_model_upgrade(tmp_path, section, enabled):
    config = load_config(config_file(tmp_path, section))
    assert config.vision.enabled is enabled and config.llm.model == "test"
    if enabled:
        assert (config.vision.max_bytes, config.vision.max_pixels, config.vision.max_dimension) == (
            100,
            50,
            10,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"enabled": 1},
        {"max_bytes": True},
        {"max_bytes": 10485761},
        {"max_pixels": -1},
        {"max_pixels": 64000001},
        {"max_dimension": 16385},
    ],
)
def test_direct_vision_config_uses_same_limits_as_decoder(change):
    with pytest.raises(ConfigError):
        VisionConfig(**change)


@pytest.mark.parametrize(
    "section",
    [
        "[vision]\nmax_bytes=true",
        "[vision]\nmax_pixels=0",
        "[vision]\nallow_any_path=true",
        "[vision]\nmodel_supports_images=true",
    ],
)
def test_toml_cannot_grant_new_paths_or_declare_model_vision(tmp_path, section):
    with pytest.raises(ConfigError):
        load_config(config_file(tmp_path, section))


def test_disabled_vision_starts_and_chats_without_importing_capability_or_sdk(tmp_path):
    """独立进程禁止可选导入，排除当前 pytest 已导入 Pillow 对结果的干扰。"""
    code = """
import builtins
import httpx
from pathlib import Path
from adam.bootstrap import build_application
from adam.llm.client import LLMClient
from adam.session.storage import SessionStore
from tests.features.test_memory_context import config_at
from tests.features.test_tool_loop import answer
original = builtins.__import__
def reject(name, *args, **kwargs):
    if name == 'PIL' or name.startswith('PIL.') or name.startswith('adam.vision'):
        raise AssertionError('optional image backend imported while disabled')
    return original(name, *args, **kwargs)
builtins.__import__ = reject
root = Path(__import__('sys').argv[1])
config = config_at(root)
with SessionStore(config.sessions_dir) as store, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer('text works')))) as http:
    app = build_application(config, store, LLMClient(config.llm, http), root)
    try:
        assert app.send('ordinary text') == 'text works'
    finally:
        app.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
