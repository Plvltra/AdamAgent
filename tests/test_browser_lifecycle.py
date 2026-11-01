"""C02.3 固定引用、资源配置和完整标签身份分页边界；真实生命周期见 features。"""

import json
from contextlib import ExitStack

import pytest

import adam.tool_calls.browser_tools as binding
from adam.browser.models import ObservationLimits
from adam.config.settings import BrowserConfig, ToolLimits
from adam.contracts.errors import ConfigError
from adam.tool_calls.factory import create_tool_registry


@pytest.mark.parametrize(
    "values",
    [
        {"max_tabs": 0},
        {"max_tabs": 33},
        {"max_tabs": True},
        {"max_contexts": 0},
        {"max_contexts": 17},
        {"max_contexts": False},
        {"max_download_bytes": 0},
        {"max_download_bytes": 100000001},
        {"max_download_bytes": True},
    ],
)
def test_browser_lifecycle_limits_reject_unbounded_or_boolean_values(values):
    with pytest.raises(ConfigError):
        BrowserConfig(**values)
    if "max_contexts" not in values:
        with pytest.raises(ValueError):
            ObservationLimits(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "C:/private/out.bin"},
        {"url": "https://example.test/file"},
        {"filename": "../outside.bin"},
        {"element_id": "a[href]"},
        {"observation_id": 3},
    ],
)
def test_download_schema_rejects_paths_or_synthetic_selectors_before_hook(
    tmp_path, monkeypatch, changes
):
    monkeypatch.setattr(binding, "open_browser", lambda *_a, **_k: pytest.fail("unexpected SDK"))
    with ExitStack() as resources:
        registry, _ = binding.register_browser(
            create_tool_registry(tmp_path),
            BrowserConfig(True, ("https://example.test",)),
            lambda: "a" * 8,
            resources,
            lambda _: pytest.fail("unexpected approval"),
        )
        args = dict.fromkeys(("browser_id", "tab_id", "observation_id", "element_id"), "a" * 32)
        args.update(changes)
        result = json.loads(
            registry.execute(
                "browser_download",
                json.dumps(args),
                before_tool=lambda *_: pytest.fail("invalid arguments reached Hook"),
            )
        )
        assert not result["ok"]


def test_tab_pages_preserve_complete_identities_and_exact_next_offset():
    source = [
        dict(
            tab_id=f"{number:032x}",
            url="https://example.test/" + "long" * 200,
            title="Long title" * 100,
            active=number == 0,
        )
        for number in range(12)
    ]
    found, offset = [], 0
    while True:
        page = binding._bounded(
            dict(
                ok=True,
                status="listed",
                browser_id="a" * 32,
                tabs=[dict(tab) for tab in source[offset:]],
                offset=offset,
                next_offset=None,
            ),
            512,
        )
        assert len(json.dumps(page, ensure_ascii=False)) <= 512
        assert page["tabs"] and page["details_truncated"] and "preview" not in page
        found.extend(tab["tab_id"] for tab in page["tabs"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page["tabs"])
        offset = page["next_offset"]
    assert found == [tab["tab_id"] for tab in source]


def test_disabled_lifecycle_keeps_registry_and_has_no_resource_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(
        binding, "BrowserTools", lambda *_a, **_k: pytest.fail("disabled owner constructed")
    )
    registry = create_tool_registry(tmp_path, limits=ToolLimits())
    with ExitStack() as resources:
        result, owner = binding.register_browser(registry, BrowserConfig(), lambda: None, resources)
        assert result is registry and owner is None


def test_new_browser_resource_limits_load_from_toml(tmp_path):
    from adam.config.loader import load_config

    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="unused"', encoding="utf-8"
    )
    path = tmp_path / "config.toml"
    path.write_text(
        "[browser]\nmax_tabs=3\nmax_contexts=2\nmax_download_bytes=4096", encoding="utf-8"
    )
    browser = load_config(path).browser
    assert (browser.max_tabs, browser.max_contexts, browser.max_download_bytes) == (3, 2, 4096)
