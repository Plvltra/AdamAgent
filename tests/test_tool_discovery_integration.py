"""T12.2 适配边界：配置、完整分页及可选请求记录。"""

import json

import pytest

from adam.config.loader import load_config
from adam.config.settings import ToolDiscoveryConfig, ToolLimits
from adam.contracts.errors import ConfigError
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.contracts.tool_catalog import validate_tool_schema_record
from adam.tool_calls.catalog import schema
from adam.tool_calls.discovery_tools import register_discovery
from adam.tool_calls.registry import ToolRegistry
from tests.test_config import LLM


@pytest.mark.parametrize(
    "section,expected",
    [
        ("", "on_demand"),
        ('[tool_discovery]\nmode="full"', "full"),
        ('[tool_discovery]\nmode="on_demand"', "on_demand"),
    ],
)
def test_loading_mode_from_toml(tmp_path, section, expected):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    main = tmp_path / "config.toml"
    main.write_text(section, encoding="utf-8")
    assert load_config(main).tool_discovery.mode == expected


@pytest.mark.parametrize(
    "section",
    [
        '[tool_discovery]\nmode="auto"',
        "[tool_discovery]\nmode=true",
        "[tool_discovery]\nunknown=1",
        "tool_discovery=[]",
    ],
)
def test_invalid_discovery_settings_fail_before_startup(tmp_path, section):
    (tmp_path / "llm.toml").write_text(LLM, encoding="utf-8")
    main = tmp_path / "config.toml"
    main.write_text(section, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(main)


def test_catalog_pages_fit_encoded_output_without_losing_entries():
    specs = {f"tool_{n}": schema('中文"\\' * 50, {}, []) for n in range(12)}
    registry = ToolRegistry(
        specs,
        dict.fromkeys(specs, lambda *_: pytest.fail("listing executed a tool")),
        ToolLimits(max_result_chars=512),
        capabilities=dict.fromkeys(specs, ToolCapability.READ_ONLY),
        groups=dict.fromkeys(specs, "mcp"),
    )
    registry, discovery = register_discovery(
        registry,
        ToolDiscoveryConfig(),
        lambda: ExecutionMode.ACT,
    )
    discovery.definitions("turn")
    offset, found = 0, []
    while offset is not None:
        raw = registry.execute("list_tools", json.dumps({"offset": offset, "limit": 100}))
        page = json.loads(raw)
        assert len(raw) <= 512 and page["ok"] and not page.get("truncated")
        found.extend(item["name"] for item in page["tools"])
        offset = page["next_offset"]
    assert found == list(specs)
    assert [tool["function"]["name"] for tool in discovery.definitions("turn")] == [
        "list_tools",
        "discover_tools",
    ]


@pytest.mark.parametrize(
    "record",
    [
        {"names": ["read_file", "read_file"], "utf8_bytes": 20},
        {"names": [False], "utf8_bytes": 2},
        {"names": [], "utf8_bytes": True},
        {"names": [], "utf8_bytes": 1},
        {"names": [], "utf8_bytes": 2, "unknown": 0},
    ],
)
def test_invalid_actual_tool_metadata_rejected(record):
    with pytest.raises(ValueError):
        validate_tool_schema_record(record)
