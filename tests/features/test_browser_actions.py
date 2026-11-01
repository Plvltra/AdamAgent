"""C02.2：模拟模型 HTTP，通过生产组装和真实 Chrome 操作临时表单。"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import httpx
import pytest

from adam.bootstrap import build_application
from adam.browser.adapters.chromium import ChromiumDriver
from adam.browser.models import BrowserError
from adam.browser.service import BrowserObserver
from adam.config.settings import AgentConfig, BrowserConfig, ToolDiscoveryConfig, ToolLimits
from adam.contracts.browser import BrowserActionUnknownError
from adam.contracts.errors import ApprovalDeniedError, BudgetError, RequestInterrupted, StorageError
from adam.llm.client import LLMClient
from adam.recording.runtime import replay_turn
from adam.session.storage import SessionStore
from tests.features.test_subagents import application as delegated_application
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import names, settings
from tests.features.test_tool_hooks import hooks_at
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions

FORM = b"""<!doctype html><title>Test form</title><h1>Temporary record</h1>
<form action="/submit" method="post"><label>Name <input name="name" aria-label="Name"></label>
<select name="kind" aria-label="Kind"><option value="a">Alpha</option><option value="b">Beta</option></select>
<button>Save record</button></form><input type="password" aria-label="Password"><input type="file" aria-label="File">
<div role="button" aria-label="Scroll region" style="height:40px;overflow:auto"><div style="height:300px">Scroll contents</div></div>
<script>const region = document.querySelector('[aria-label="Scroll region"]');
region.addEventListener('scroll', () => fetch('/scroll?y=' + region.scrollTop));</script>"""


@pytest.fixture
def action_site():
    """只使用本机站点，独立记录真实 POST 正文和滚动事件；无外部发送。"""
    posts, requests, scrolls = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/scroll?"):
                scrolls.append(parse_qs(self.path.split("?", 1)[1])["y"][0])
            body = FORM
            if self.path == "/receipt":
                body = (
                    "<h1>Saved receipt</h1><p>Name="
                    + posts[-1]["name"][0]
                    + "; Kind="
                    + posts[-1]["kind"][0]
                    + "</p>"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            posts.append(
                parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode(),
                    keep_blank_values=True,
                )
            )
            self.send_response(307 if "redirect=307" in self.path else 303)
            mode = parse_qs(self.path.partition("?")[2]).get("redirect", [""])[0]
            self.send_header(
                "Location",
                f"http://localhost:{server.server_port}/leak" if mode == "cross" else "/receipt",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", posts, requests, scrolls
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def configured(root, origin, **changes):
    """测试显式授权唯一临时 origin，并使用已安装 Chrome，不下载浏览器。"""
    changes.setdefault("tool_discovery", ToolDiscoveryConfig(mode="full"))
    return settings(
        root,
        browser=BrowserConfig(enabled=True, allowed_origins=(origin,), channel="chrome"),
        **changes,
    )


@contextmanager
def application(root, respond, config, approve=None, **kwargs):
    """保留生产客户端/组装/JSONL，仅模型 HTTP 使用 MockTransport。"""
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=respond(body, len(bodies)))

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
    ):
        app = build_application(
            config, store, LLMClient(config.llm, http), root, confirm_browser=approve, **kwargs
        )
        try:
            yield app, store, bodies
        finally:
            app.close()


def action(body, label, operation="click", **values):
    """只从模型实际收到的最新观察取得控件身份。"""
    observation = outputs(body)[-1]["observation"]
    control = next(item for item in observation["elements"] if item["name"] == label)
    return call(
        "act-" + observation["observation_id"],
        "browser_action",
        **control["reference"],
        action=operation,
        **values,
    )


def test_real_form_fill_select_scroll_click_has_one_post_receipt_and_logs(tmp_path, action_site):
    origin, posts, requests, scrolls = action_site
    approved = []

    def approve(plan):
        approved.append(plan)
        return True

    def respond(body, number):
        if number == 1:
            assert names(body) == ["list_tools", "discover_tools"]
            return tool_response(
                [
                    call(
                        "discover",
                        "discover_tools",
                        names=["browser_open", "browser_observe", "browser_action"],
                    )
                ]
            )
        if number == 2:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 3:
            return tool_response([action(body, "Name", "fill", value="Local Alice")])
        if number == 4:
            assert outputs(body)[-1]["status"] == "dispatched"
            return tool_response([action(body, "Kind", "select", value="b")])
        if number == 5:
            return tool_response([action(body, "Scroll region", "scroll", scroll_y=100)])
        if number == 6:
            return tool_response([action(body, "Save record")])
        result = outputs(body)[-1]
        assert result["status"] == "dispatched"
        assert result["observation"]["url"] == origin + "/receipt"
        assert "Name=Local Alice; Kind=b" in result["observation"]["text"]
        return answer("Saved receipt confirms Local Alice / b")

    config = configured(tmp_path, origin, tool_discovery=ToolDiscoveryConfig())
    with application(tmp_path, respond, config, approve) as (app, store, bodies):
        assert app.send("保存一条临时测试记录") == "Saved receipt confirms Local Alice / b"
        sid = app.session.session_id
        events = store.load_events(sid)
        results = [
            json.loads(e["payload"]["output"]) for e in events if e["type"] == "tool_finished"
        ]
        assert [r["status"] for r in results[1:]] == ["opened", *["dispatched"] * 4]
        assert len(bodies) == 7
        assert app.execution_report().reason == "completed"
    with SessionStore(config.sessions_dir) as restored:
        assert restored.load_events(sid) == events
    assert [plan.operation for plan in approved] == ["open", "fill", "select", "scroll", "click"]
    assert approved[1].value == "Local Alice" and approved[2].value == "b"
    assert all(plan.reference for plan in approved[1:])
    assert len({plan.reference[2] for plan in approved[1:]}) == 4
    assert posts == [{"name": ["Local Alice"], "kind": ["b"]}]
    assert requests.count("/receipt") == 1 and scrolls == ["100"]


def test_default_denial_stops_batch_before_resource_creation(tmp_path, action_site):
    origin, posts, requests, _ = action_site
    response = tool_response(
        [call("one", "browser_open", url=origin), call("two", "browser_open", url=origin)]
    )
    with application(tmp_path, lambda *_: response, configured(tmp_path, origin)) as (
        app,
        store,
        bodies,
    ):
        with pytest.raises(ApprovalDeniedError):
            app.send("open")
        finished = [
            e for e in store.load_events(app.session.session_id) if e["type"] == "tool_finished"
        ]
        assert len(finished) == len(bodies) == 1
        assert json.loads(finished[0]["payload"]["output"])["status"] == "denied"
    assert requests == posts == []


@pytest.mark.parametrize("boundary", ["discovery", "plan", "hook", "budget"])
def test_original_preflight_boundaries_block_before_approval_or_browser(
    tmp_path, action_site, boundary
):
    origin, posts, requests, _ = action_site
    changes = {}
    calls = [call("one", "browser_open", url=origin)]
    if boundary == "discovery":
        changes["tool_discovery"] = ToolDiscoveryConfig()
    elif boundary == "hook":
        changes["hooks"] = hooks_at(tmp_path, before="deny")
    elif boundary == "budget":
        changes["agent"] = AgentConfig(max_tool_calls=1)
        calls.append(call("two", "browser_open", url=origin))

    def respond(body, number):
        if number == 1:
            return tool_response(calls)
        result = outputs(body)[-1]
        assert not result["ok"]
        if boundary in {"plan", "hook"}:
            assert result["status"] == ("mode_blocked" if boundary == "plan" else "hook_blocked")
        return answer("blocked")

    with application(
        tmp_path,
        respond,
        configured(tmp_path, origin, **changes),
        lambda _: pytest.fail("unexpected approval"),
    ) as (app, _, _):
        if boundary == "plan":
            app.select_mode("plan")
        if boundary == "budget":
            with pytest.raises(BudgetError):
                app.send("open")
        else:
            assert app.send("open") == "blocked"
    assert requests == posts == []


@pytest.mark.parametrize("label", ["Password", "File"])
def test_sensitive_controls_rejected_without_action_approval(tmp_path, action_site, label):
    origin, posts, _, _ = action_site
    approvals = []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([action(body, label, "fill", value="forbidden")])
        assert outputs(body)[-1]["status"] == "unsupported_control"
        return answer("refused sensitive control")

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda plan: approvals.append(plan) or True
    ) as (app, _, _):
        assert app.send("fill") == "refused sensitive control"
    assert [p.operation for p in approvals] == ["open"] and posts == []


def test_old_reference_after_reobserve_is_not_reapproved(tmp_path, action_site):
    origin, posts, _, _ = action_site
    saved, approved = [], []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            saved.append(action(body, "Save record"))
            return tool_response([call("observe", "browser_observe")])
        if number == 3:
            return tool_response(saved)
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("need fresh evidence")

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda p: approved.append(p) or True
    ) as (app, _, _):
        assert app.send("act with old evidence") == "need fresh evidence"
    assert len(approved) == 1 and posts == []


def test_dom_replaced_during_approval_is_rechecked_before_dispatch(
    tmp_path, action_site, monkeypatch
):
    origin, posts, _, _ = action_site
    drivers = []

    def capture(driver, snapshot, action, cancel):
        pytest.fail("replacement should be rejected before driver action")

    original_start = ChromiumDriver._start

    async def start(driver, channel):
        await original_start(driver, channel)
        drivers.append(driver)

    monkeypatch.setattr(ChromiumDriver, "_start", start)
    monkeypatch.setattr(ChromiumDriver, "act", capture)

    def approve(plan):
        if plan.operation == "click":
            driver = drivers[0]
            # 注入真实 DOM 替换，模拟用户确认期间网页变化，不替换生产引用校验。
            driver._runner.run(
                driver._page.evaluate(
                    "document.querySelector('button').outerHTML = '<button>Save record</button>'"
                )
            )
        return True

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([action(body, "Save record")])
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("changed while waiting")

    with application(tmp_path, respond, configured(tmp_path, origin), approve) as (app, _, _):
        assert app.send("save") == "changed while waiting"
    assert posts == []


@pytest.mark.parametrize("boundary", ["start_log", "result_log", "sdk_after_post", "os_after_post"])
def test_failure_boundaries_stop_without_automatic_resubmit(
    tmp_path, action_site, monkeypatch, boundary
):
    origin, posts, _, _ = action_site
    original_act = ChromiumDriver.act

    def fail_after_dispatch(driver, snapshot, action, cancel):
        original_act(driver, snapshot, action, cancel)
        # 故障发生在真实 POST 已被服务器接受之后，不能宣称动作未发生。
        if boundary == "os_after_post":
            raise OSError("injected SDK pipe failure after real dispatch")
        raise BrowserError("browser_failed", "injected lost SDK result")

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([action(body, "Save record")])
        pytest.fail("uncertain or unlogged action must stop before another model request")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        store,
        bodies,
    ):
        original_append = store.append_tool_event

        def append(sid, kind, payload):
            is_action = (
                payload.get("name") == "browser_action"
                if kind == "tool_started"
                else payload["tool_call_id"].startswith("act-")
            )
            if (
                is_action
                and kind == ("tool_started" if boundary == "start_log" else "tool_finished")
                and boundary not in {"sdk_after_post", "os_after_post"}
            ):
                raise StorageError("injected tool log failure")
            return original_append(sid, kind, payload)

        monkeypatch.setattr(store, "append_tool_event", append)
        if boundary in {"sdk_after_post", "os_after_post"}:
            monkeypatch.setattr(ChromiumDriver, "act", fail_after_dispatch)
        with pytest.raises(
            BrowserActionUnknownError
            if boundary in {"sdk_after_post", "os_after_post"}
            else StorageError
        ):
            app.send("save")
        assert len(bodies) == 2
        assert app.execution_report().status == "failed"
    assert len(posts) == (0 if boundary == "start_log" else 1)


@pytest.mark.parametrize(
    "fault",
    [
        BrowserError("browser_failed", "injected observation-only failure"),
        OSError("injected observation pipe failure"),
    ],
)
def test_post_action_observation_failure_keeps_dispatched_evidence(
    tmp_path, action_site, monkeypatch, fault
):
    origin, posts, _, _ = action_site
    original = BrowserObserver.observe
    fail_next = False
    original_perform = BrowserObserver.perform

    def observe(browser, **kwargs):
        if fail_next:
            raise fault
        return original(browser, **kwargs)

    def perform(browser, action, **kwargs):
        nonlocal fail_next
        fail_next = True
        return original_perform(browser, action, **kwargs)

    monkeypatch.setattr(BrowserObserver, "observe", observe)
    monkeypatch.setattr(BrowserObserver, "perform", perform)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([action(body, "Save record")])
        assert outputs(body)[-1]["status"] == "dispatched"
        assert outputs(body)[-1]["observation_unavailable"]
        return answer("dispatched, requires human verification")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        _,
    ):
        assert app.send("save") == "dispatched, requires human verification"
    assert len(posts) == 1


def test_cross_session_reference_cannot_operate_other_context(tmp_path, action_site):
    origin, posts, _, _ = action_site
    saved, approved = [], []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            saved.append(action(body, "Save record"))
            return answer("observed first session")
        if number == 3:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 4:
            return tool_response(saved)
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("different owner")

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda p: approved.append(p) or True
    ) as (app, _, _):
        app.send("first")
        first = app.session.session_id
        app.new()
        assert app.session.session_id != first
        assert app.send("second") == "different owner"
    assert len(approved) == 2 and posts == []


def test_small_output_keeps_structured_reference_and_dispatch_status(tmp_path, action_site):
    origin, posts, _, _ = action_site

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        result = outputs(body)[-1]
        assert len(json.dumps(result, ensure_ascii=False)) <= 1024
        assert "preview" not in result and result["status"] == "opened"
        assert result["observation"]["elements_truncated"]
        assert result["observation"]["elements"][0]["reference"]["browser_id"]
        return answer("bounded")

    with application(
        tmp_path,
        respond,
        configured(tmp_path, origin, tool_limits=ToolLimits(max_result_chars=1024)),
        lambda _: True,
    ) as (app, _, _):
        assert app.send("observe") == "bounded"
    assert posts == []


@pytest.mark.parametrize("decision", ["deny", "cancel"])
def test_action_denial_or_cancellation_stops_pending_batch_and_closes(
    tmp_path, action_site, monkeypatch, decision
):
    origin, posts, _, _ = action_site
    closed = []
    original_close = ChromiumDriver.close

    def close(driver):
        closed.append(driver)
        return original_close(driver)

    monkeypatch.setattr(ChromiumDriver, "close", close)

    def approve(plan):
        if plan.operation == "click":
            if decision == "cancel":
                raise RequestInterrupted()
            return False
        return True

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response(
                [action(body, "Save record"), call("later", "browser_open", url=origin)]
            )
        pytest.fail("denied/cancelled batch must stop")

    with application(tmp_path, respond, configured(tmp_path, origin), approve) as (app, _, bodies):
        with pytest.raises(RequestInterrupted if decision == "cancel" else ApprovalDeniedError):
            app.send("save")
        assert len(bodies) == 2
        if decision == "cancel":
            assert len(closed) == 1
    assert len(closed) == 1 and posts == []


def test_duplicate_dispatched_reference_is_rejected_before_second_approval(tmp_path, action_site):
    origin, posts, _, _ = action_site
    saved, approvals = [], []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            saved.append(action(body, "Save record"))
            return tool_response(saved)
        if number == 3:
            assert outputs(body)[-1]["status"] == "dispatched"
            # 模拟模型在收到结果后误用同一引用，而不是服务器重复回执。
            old = saved[0]
            return tool_response([("duplicate", old[1], old[2])])
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("already dispatched; no retry")

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda p: approvals.append(p) or True
    ) as (app, _, _):
        assert app.send("save once") == "already dispatched; no retry"
    assert len(posts) == 1 and len(approvals) == 2


@pytest.mark.parametrize("redirect", ["cross", "307"])
def test_action_redirect_denial_never_reposts_or_follows_unapproved_origin(
    tmp_path, action_site, monkeypatch, redirect
):
    origin, posts, requests, _ = action_site
    # 改变测试站点实际表单目标，不替换生产路由；真实 POST 的 3xx 仍由适配器处理。
    monkeypatch.setattr(
        __import__(__name__, fromlist=["FORM"]),
        "FORM",
        FORM.replace(b'action="/submit"', ('action="/submit?redirect=' + redirect + '"').encode()),
    )

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([action(body, "Save record")])
        pytest.fail("denied action navigation has uncertain effects and must stop")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        bodies,
    ):
        with pytest.raises(BrowserActionUnknownError):
            app.send("save")
        assert len(bodies) == 2
    assert len(posts) == 1 and "/receipt" not in requests and "/leak" not in requests


def test_recorded_action_replays_without_browser_or_second_post(tmp_path, action_site, monkeypatch):
    origin, posts, _, _ = action_site

    def respond(body, number):
        if number == 1:
            result = tool_response([call("open", "browser_open", url=origin)])
        elif number == 2:
            result = tool_response([action(body, "Save record")])
        else:
            assert "Saved receipt" in outputs(body)[-1]["observation"]["text"]
            result = answer("recorded receipt")
        return httpx.Response(200, json=result)

    with captured_app(
        tmp_path, respond, config=configured(tmp_path, origin), confirm_browser=lambda _: True
    ) as (app, recorder, _, source, _):
        assert recorder.run(lambda: app.send("task")) == "recorded receipt"
    original = source.read_bytes()
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        ChromiumDriver,
        "__init__",
        lambda *_a, **_k: pytest.fail("offline replay launched a browser"),
    )
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert result["reply"] == "recorded receipt" and source.read_bytes() == original
    assert len(posts) == 1


def test_child_does_not_inherit_parent_browser_or_interactive_approval(tmp_path, action_site):
    origin, posts, requests, _ = action_site
    parent_count = 0

    def parent(request):
        nonlocal parent_count
        parent_count += 1
        if parent_count == 1:
            result = tool_response(
                [call("spawn", "spawn_agent", name="browser-check", task="check available tools")]
            )
        elif parent_count == 2:
            result = tool_response([call("wait", "wait_agents", agent_ids=[])])
        else:
            assert outputs(json.loads(request.content))[-1]["agents"][0]["reply"] == "no browser"
            result = answer("child isolated")
        return httpx.Response(200, json=result)

    def child(request):
        assert not any(name.startswith("browser_") for name in names(json.loads(request.content)))
        return httpx.Response(200, json=answer("no browser"))

    with delegated_application(tmp_path, parent, child, config=configured(tmp_path, origin)) as (
        app,
        _,
    ):
        assert app.send("delegate") == "child isolated"
    assert requests == posts == []
