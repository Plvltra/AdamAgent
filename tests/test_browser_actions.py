"""C02.2 配置、固定动作协议、逐次批准展示和可取消串行注册边界。"""

import json
from contextlib import ExitStack
from dataclasses import asdict

import pytest

import adam.tool_calls.browser_tools as binding
from adam.browser.models import BrowserAction, BrowserError, ElementReference
from adam.cli.main import confirm_browser
from adam.config.loader import load_config
from adam.config.settings import BrowserConfig, ToolLimits
from adam.contracts.browser import BrowserApproval
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ConfigError, RequestInterrupted
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.tool_calls.catalog import schema
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.registry import ToolRegistry


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": 1},
        {"enabled": True},
        {"allowed_origins": "https://example.test"},
        {"allowed_origins": [1]},
        {"channel": []},
        {"channel": "firefox"},
        {"max_chars": True},
        {"max_elements": 0},
        {"max_elements": 501},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
    ],
)
def test_invalid_browser_configuration_fails_closed(values):
    with pytest.raises(ConfigError):
        BrowserConfig(**values)


def test_browser_toml_parses_and_rejects_unknown_authority_fields(tmp_path):
    (tmp_path / "llm.toml").write_text(
        '[llm]\nbase_url="https://unused.test"\nmodel="test"\napi_key="unused"', encoding="utf-8"
    )
    path = tmp_path / "config.toml"
    path.write_text(
        '[browser]\nenabled=true\nallowed_origins=["https://example.test"]\nchannel="chrome"\nmax_chars=800\nmax_elements=5\ntimeout_seconds=2.5',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.browser == BrowserConfig(True, ("https://example.test",), "chrome", 800, 5, 2.5)
    path.write_text("[browser]\nallow_all=true", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "eval"},
        {"value": "unexpected"},
        {"scroll_y": 1},
        {"operation": "fill"},
        {"operation": "fill", "value": "\ud800"},
        {"operation": "select", "value": "x" * 4001},
        {"operation": "scroll", "scroll_y": True},
        {"operation": "scroll", "scroll_y": 0},
        {"operation": "scroll", "scroll_y": 2001},
        {"reference": ElementReference("selector", "a", "b", "c")},
    ],
)
def test_action_protocol_has_no_arbitrary_code_or_unbounded_values(values):
    arguments = {"operation": "click", "reference": ElementReference(*["a" * 32] * 4)}
    arguments.update(values)
    with pytest.raises(BrowserError):
        BrowserAction(**arguments)


@pytest.mark.parametrize(
    "suffix", [{"javascript": "alert(1)"}, {"selector": "#save"}, {"value": 3}, {"action": "eval"}]
)
def test_action_schema_rejects_bad_arguments_before_hook_or_approval(tmp_path, suffix):
    with ExitStack() as resources:
        registry = binding.register_browser(
            create_tool_registry(tmp_path),
            BrowserConfig(True, ("https://example.test",)),
            lambda: "abcd1234",
            resources,
            lambda _: pytest.fail("approval bypass"),
        )
        arguments = dict.fromkeys(
            ("browser_id", "tab_id", "observation_id", "element_id"), "a" * 32
        )
        arguments.update(action="fill", value="x")
        arguments.update(suffix)
        result = json.loads(
            registry.execute(
                "browser_action",
                json.dumps(arguments),
                before_tool=lambda *_: pytest.fail("invalid arguments reached Hook"),
            )
        )
        assert not result["ok"]


def test_enabled_but_unused_and_invalid_origin_never_launch_sdk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        binding, "open_browser", lambda *_a, **_k: pytest.fail("unexpected SDK resource")
    )
    with ExitStack() as resources:
        registry = binding.register_browser(
            create_tool_registry(tmp_path),
            BrowserConfig(True, ("https://example.test",)),
            lambda: "abcd1234",
            resources,
            lambda _: pytest.fail("invalid origin reached approval"),
        )
        for name in binding.SPECS:
            assert registry.capability(name) is ToolCapability.EXTERNAL_ACTION
            assert not registry.can_parallelize(name)
        result = json.loads(registry.execute("browser_open", '{"url":"https://other.test/"}'))
        assert result["status"] == "network_denied"
        assert json.loads(registry.execute("browser_observe", "{}"))["status"] == "not_open"


def test_cancel_or_session_change_after_approval_does_not_construct_resources(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        binding, "open_browser", lambda *_a, **_k: pytest.fail("unapproved resource")
    )
    session = ["abcd1234"]
    cancel = CancellationToken()

    def approve(plan):
        cancel.cancel()
        return True

    with ExitStack() as resources:
        registry = binding.register_browser(
            create_tool_registry(tmp_path),
            BrowserConfig(True, ("https://example.test",)),
            lambda: session[0],
            resources,
            approve,
        )
        with pytest.raises(RequestInterrupted):
            registry.execute("browser_open", '{"url":"https://example.test/"}', cancel=cancel)
    with ExitStack() as resources:
        registry = binding.register_browser(
            create_tool_registry(tmp_path),
            BrowserConfig(True, ("https://example.test",)),
            lambda: session[0],
            resources,
            lambda _: session.__setitem__(0, "1234abcd") or True,
        )
        result = json.loads(registry.execute("browser_open", '{"url":"https://example.test/"}'))
        assert result["status"] == "stale_reference"


def test_interruptible_serial_handlers_keep_token_recorder_and_wrappers():
    calls, records = [], []
    token = CancellationToken()

    # 保存绑定方法身份，避免每次属性访问创建新的 bound method。
    recorder = records.append

    def invoke(args, record, cancel):
        assert cancel is token and record is recorder
        calls.append(args)
        return {"status": "serial"}

    registry = ToolRegistry(
        {"probe": schema("probe", {}, [])},
        {"probe": lambda *_: pytest.fail("legacy handler used")},
        ToolLimits(),
        interruptible_handlers={"probe": invoke},
        capabilities={"probe": ToolCapability.EXTERNAL_ACTION},
    )
    registry = (
        registry.guarded(lambda _: None)
        .with_mode(lambda: ExecutionMode.ACT)
        .extended({"extra": schema("extra", {}, [])}, {"extra": lambda *_: {}})
    )
    assert not registry.can_parallelize("probe")
    assert (
        json.loads(registry.execute("probe", "{}", record_execution=recorder, cancel=token))[
            "status"
        ]
        == "serial"
    )
    assert calls == [{}]


@pytest.mark.parametrize("where", ["constructor", "extension"])
@pytest.mark.parametrize("bad", ["unknown", "not_callable", "overlap"])
def test_invalid_serial_registration_cannot_borrow_parallel_or_existing_tool(where, bad):
    specs, handlers = {"probe": schema("probe", {}, [])}, {"probe": lambda *_: {}}
    kwargs = {
        "interruptible_handlers": {
            "unknown" if bad == "unknown" else "probe": None
            if bad == "not_callable"
            else lambda *_: {}
        }
    }
    if bad == "overlap":
        kwargs.update(
            capabilities={"probe": ToolCapability.READ_ONLY},
            parallel_handlers={"probe": lambda *_: {}},
        )
    with pytest.raises(ValueError):
        if where == "constructor":
            ToolRegistry(specs, handlers, ToolLimits(), **kwargs)
        else:
            ToolRegistry({}, {}, ToolLimits()).extended(specs, handlers, **kwargs)


@pytest.mark.parametrize(
    "reply,approved", [("y", True), (" Y ", True), ("yes", False), ("", False)]
)
def test_cli_approval_escapes_untrusted_text_and_shows_exact_values(reply, approved):
    plan = BrowserApproval(
        "fill", "https://example.test/?x=1", "Name\n\x1b[31m", tuple(["a" * 32] * 4), "new\nvalue"
    )
    shown = []
    assert confirm_browser(plan, lambda _: reply, shown.append) is approved
    assert json.loads(shown[0].split("> ", 1)[1]) == json.loads(json.dumps(asdict(plan)))
    assert "\x1b" not in shown[0] and "\n" not in shown[0]


def test_cli_eof_denies_and_ctrl_c_remains_cancellation():
    def eof(_):
        raise EOFError

    def interrupt(_):
        raise KeyboardInterrupt

    plan = BrowserApproval("open", "https://example.test")
    assert not confirm_browser(plan, eof, lambda _: None)
    with pytest.raises(KeyboardInterrupt):
        confirm_browser(plan, interrupt, lambda _: None)
