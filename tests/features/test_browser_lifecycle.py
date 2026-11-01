"""C02.3：真实临时网站、Chrome 标签/弹窗/下载及所属会话的停止边界。"""

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import Error as PlaywrightError

from adam.browser.adapters.chromium import ChromiumDriver
from adam.browser.models import BrowserAction, BrowserError
from adam.browser.policy import BrowserPolicy
from adam.browser.runtime import open_browser
from adam.config.settings import AgentConfig
from adam.contracts.browser import BrowserActionUnknownError
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ApprovalDeniedError, RequestInterrupted, StorageError
from adam.recording.runtime import replay_turn
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from adam.session.storage import SessionStore
from adam.tool_calls.browser_tools import BrowserTools
from tests.features.test_browser_actions import application, configured
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response
from tests.features.test_turn_replay import captured_app, forbid_external_actions

BINARY = b"complete fixture attachment\x00\xff"
PAGE = b"""<!doctype html><title>Owner</title><h1>Owner page</h1>
<a href="/child" target="_blank">Open child</a>
<a href="/child#part" target="_blank">Fragment child</a>
<a href="/file">Download attachment</a>
<a href="/file-redirect">Redirect attachment</a>
<a href="/large">Large attachment</a>
<a href="/slow-file">Slow attachment</a>
<a href="/bad-mime">Bad MIME</a>
<a href="/not-attachment">Not attachment</a>
<a href="http://localhost:{port}/leak" target="_blank">Denied popup</a>
<button onclick="fetch('/commit',{method:'POST'}).then(()=>document.body.dataset.committed='yes')">Commit once</button>
<button onclick="if(confirm('Not an approval'))fetch('/commit',{method:'POST'})">Confirm submit</button>
<button onclick="alert('Untrusted notice')">Show alert</button>
"""


@pytest.fixture
def lifecycle_site():
    """只允许唯一临时 origin；独立记录服务器收到的真实请求，不访问外网。"""
    requests, posts = [], []
    ready, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = PAGE.replace(b"{port}", str(server.server_port).encode())
            if self.path == "/child":
                body = b"<title>Child</title><h1>Child evidence</h1><button>Child control</button>"
            headers = (
                {"Set-Cookie": "fixture_download=private-download-cookie; Path=/"}
                if self.path == "/"
                else {}
            )
            if self.path in {"/file", "/large", "/slow-file", "/bad-mime"}:
                body = BINARY * 100 if self.path == "/large" else BINARY
                headers = {
                    "Content-Disposition": 'attachment; filename="../../untrusted.bin"',
                    "Content-Type": "application/octet-stream",
                }
            if self.path == "/bad-mime":
                headers["Content-Type"] = "image/*"
            status = 200
            if self.path == "/file-redirect":
                status, body = 302, b""
                headers["Location"] = "/file"
            if (
                self.path == "/file"
                and self.headers.get("Cookie") != "fixture_download=private-download-cookie"
            ):
                status = 403
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Type", headers.pop("Content-Type", "text/html; charset=utf-8")
            )
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.path == "/slow-file":
                ready.set()
                release.wait(10)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_POST(self):
            posts.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, posts, ready, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)


def current_control(body, label, *, download=False):
    """只取模型实际收到的最新观察，不构造选择器或读取内部 Page。"""
    observation = next(
        result["observation"] for result in reversed(outputs(body)) if "observation" in result
    )
    control = next(item for item in observation["elements"] if item["name"] == label)
    return call(
        "download" if download else "act-" + observation["observation_id"],
        "browser_download" if download else "browser_action",
        **control["reference"],
        **({} if download else {"action": "click"}),
    )


def test_owned_popup_select_close_download_and_context_close(tmp_path, lifecycle_site):
    origin, requests, posts, *_ = lifecycle_site
    saved, approvals = {}, []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            saved["browser"] = outputs(body)[-1]["observation"]["browser_id"]
            return tool_response([current_control(body, "Open child")])
        if number in {3, 6}:
            return tool_response([call("tabs-" + str(number), "browser_tabs")])
        if number in {4, 7}:
            tabs = outputs(body)[-1]["tabs"]
            assert len(tabs) == (2 if number == 4 else 1)
            tab = next(tab for tab in tabs if (tab["title"] == "Child") == (number == 4))
            saved["child"] = tab["tab_id"] if number == 4 else saved["child"]
            return tool_response(
                [
                    call(
                        "select-" + str(number),
                        "browser_select_tab",
                        browser_id=saved["browser"],
                        tab_id=tab["tab_id"],
                    )
                ]
            )
        if number == 5:
            assert "Child evidence" in outputs(body)[-1]["observation"]["text"]
            return tool_response(
                [
                    call(
                        "close-child",
                        "browser_close_tab",
                        browser_id=saved["browser"],
                        tab_id=saved["child"],
                    )
                ]
            )
        if number == 8:
            return tool_response([current_control(body, "Download attachment", download=True)])
        if number == 9:
            result = outputs(body)[-1]
            assert result["status"] == "downloaded"
            saved["media"] = result["media"]
            return tool_response(
                [call("close-context", "browser_close", browser_id=saved["browser"])]
            )
        assert outputs(body)[-1]["status"] == "closed"
        return answer("owned lifecycle verified")

    config = configured(
        tmp_path, origin, agent=AgentConfig(max_model_requests=14, max_tool_calls=20)
    )
    with application(tmp_path, respond, config, lambda plan: approvals.append(plan) or True) as (
        app,
        store,
        _,
    ):
        assert app.send("temporary lifecycle") == "owned lifecycle verified"
        ref = saved["media"]
        content = SessionMedia(store, MediaStore(store.root / "_media")).read(
            ref["media_id"], expected_version=ref["version"]
        )
        assert content.data == BINARY
        assert content.reference.source.tool_name == "browser_download"
        events = store.load_events(app.session.session_id)
        assert "private-download-cookie" not in json.dumps(events)
        media = next(item for item in events if item["type"] == "media_saved")
        assert media["payload"]["version"] == hashlib.sha256(BINARY).hexdigest()
        assert media["payload"]["size_bytes"] == len(BINARY)
        assert len(list((store.root / "_media").glob("*/*.media"))) == 1
        assert not list(tmp_path.rglob("untrusted.bin"))
    assert [plan.operation for plan in approvals] == [
        "open",
        "click",
        "select_tab",
        "close_tab",
        "select_tab",
        "download",
        "close_browser",
    ]
    assert requests.count("/file") == requests.count("/child") == 1 and posts == []


def test_dialogs_are_dismissed_not_promoted_to_approval(tmp_path, lifecycle_site):
    origin, _, posts, *_ = lifecycle_site

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Confirm submit")])
        if number == 3:
            assert outputs(body)[-1]["observation"]["dismissed_dialogs"] == 1
            return tool_response([current_control(body, "Show alert")])
        assert outputs(body)[-1]["observation"]["dismissed_dialogs"] == 2
        return answer("dialogs dismissed")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        _,
    ):
        assert app.send("safe dialogs") == "dialogs dismissed"
    assert posts == []


def test_last_tab_close_requires_explicit_open_with_new_identity(lifecycle_site):
    origin, requests, _, *_ = lifecycle_site
    with open_browser(BrowserPolicy(frozenset({origin})), channel="chrome") as browser:
        first = browser.open(origin)
        plan = browser.plan_tab(
            "close_tab", first.browser_id, first.tab_id, cancel=CancellationToken()
        )
        browser.perform_tab(plan, cancel=CancellationToken())
        assert browser.tabs() == ()
        with pytest.raises(BrowserError, match="没有当前标签"):
            browser.observe()
        second = browser.open(origin)
        assert first.browser_id == second.browser_id and first.tab_id != second.tab_id
        assert len(requests) == 2


@pytest.mark.parametrize("limit", ["tabs", "contexts"])
def test_resource_caps_do_not_evict_or_implicitly_recreate(tmp_path, lifecycle_site, limit):
    origin, requests, _, *_ = lifecycle_site
    config = configured(tmp_path, origin)
    config = replace(config, browser=replace(config.browser, **{"max_" + limit: 1}))
    count = 0

    def respond(body, number):
        nonlocal count
        count += 1
        if number == 1 or limit == "contexts" and number == 3:
            return tool_response([call("open" + str(number), "browser_open", url=origin)])
        if number == 2 and limit == "contexts":
            return answer("first owner retained")
        if number == 2:
            return tool_response([current_control(body, "Open child")])
        if limit == "contexts":
            assert outputs(body)[-1]["status"] == "resource_limit"
            return answer("no new resource")
        if number == 3:
            return tool_response([call("tabs", "browser_tabs")])
        assert len(outputs(body)[-1]["tabs"]) == 1
        return answer("one original tab")

    with application(tmp_path, respond, config, lambda _: True) as (app, _, _):
        first = app.send("first")
        if limit == "contexts":
            app.new()
            assert app.send("second") == "no new resource"
        else:
            assert first == "one original tab"
    assert requests.count("/") == 1 and "/child" not in requests


def test_cross_origin_popup_is_never_requested(tmp_path, lifecycle_site):
    origin, requests, _, *_ = lifecycle_site

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Denied popup")])
        assert outputs(body)[-1]["status"] == "dispatched"
        assert outputs(body)[-1]["observation"]["blocked_requests"] >= 1
        return answer("foreign popup blocked")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        _,
    ):
        assert app.send("popup") == "foreign popup blocked"
    assert "/leak" not in requests


def test_download_denial_stops_batch_without_request_or_media(tmp_path, lifecycle_site):
    origin, requests, _, *_ = lifecycle_site

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        return tool_response(
            [
                current_control(body, "Download attachment", download=True),
                call("later", "browser_open", url=origin),
            ]
        )

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda plan: plan.operation != "download"
    ) as (app, store, bodies):
        with pytest.raises(ApprovalDeniedError):
            app.send("download")
        assert len(bodies) == 2 and not (store.root / "_media").exists()
    assert "/file" not in requests and requests.count("/") == 1


@pytest.mark.parametrize("label", ["Large attachment", "Bad MIME", "Not attachment"])
def test_incomplete_or_invalid_download_stops_without_media(tmp_path, lifecycle_site, label):
    origin, requests, _, *_ = lifecycle_site
    config = configured(tmp_path, origin)
    config = replace(
        config, browser=replace(config.browser, max_download_bytes=128, timeout_seconds=5)
    )

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, label, download=True)])
        pytest.fail("failed download must not reach another model request")

    with application(tmp_path, respond, config, lambda _: True) as (app, store, bodies):
        with pytest.raises(BrowserActionUnknownError):
            app.send("download")
        assert len(bodies) == 2 and not (store.root / "_media").exists()
    assert (
        requests.count(
            {
                "Large attachment": "/large",
                "Bad MIME": "/bad-mime",
                "Not attachment": "/not-attachment",
            }[label]
        )
        == 1
    )


@pytest.mark.parametrize("boundary", ["file_publish", "source_log", "lost_sdk_result"])
def test_download_failure_after_request_never_retries_or_delivers_reference(
    tmp_path, lifecycle_site, monkeypatch, boundary
):
    origin, requests, _, *_ = lifecycle_site

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Download attachment", download=True)])
        pytest.fail("failed publication must stop this round")

    original = ChromiumDriver.download

    def lost_result(driver, *args):
        original(driver, *args)
        raise BrowserError("browser_failed", "injected lost SDK result after actual download")

    def fail(*args, **kwargs):
        # 注入明确的正文发布或来源日志边界，真实网络请求已经完成。
        if boundary == "file_publish":
            raise PermissionError("injected file publication failure")
        raise StorageError("injected source log failure")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        store,
        bodies,
    ):
        if boundary == "file_publish":
            monkeypatch.setattr(MediaStore, "_publish", fail)
        elif boundary == "source_log":
            monkeypatch.setattr(store, "append_media", fail)
        else:
            monkeypatch.setattr(ChromiumDriver, "download", lost_result)
        with pytest.raises(
            BrowserActionUnknownError if boundary == "lost_sdk_result" else StorageError
        ):
            app.send("download")
        assert len(bodies) == 2
        assert not any(
            event["type"] == "media_saved" for event in store.load_events(app.session.session_id)
        )
        assert len(list((store.root / "_media").glob("*/*.media"))) == (
            1 if boundary == "source_log" else 0
        )
    assert requests.count("/file") == 1


def test_cancellation_during_real_download_closes_before_return(
    tmp_path, lifecycle_site, monkeypatch
):
    origin, requests, _, ready, release = lifecycle_site
    closed, workers = [], []
    original_download, original_close = ChromiumDriver.download, ChromiumDriver.close

    def download(driver, snapshot, reference, cancel):
        def interrupt():
            if ready.wait(5):
                cancel.cancel()

        worker = threading.Thread(target=interrupt)
        workers.append(worker)
        worker.start()
        return original_download(driver, snapshot, reference, cancel)

    def close(driver):
        closed.append(driver)
        return original_close(driver)

    monkeypatch.setattr(ChromiumDriver, "download", download)
    monkeypatch.setattr(ChromiumDriver, "close", close)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        return tool_response([current_control(body, "Slow attachment", download=True)])

    try:
        with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
            app,
            store,
            _,
        ):
            with pytest.raises(RequestInterrupted):
                app.send("cancel in flight")
            assert len(closed) == 1 and not (store.root / "_media").exists()
    finally:
        release.set()
        for worker in workers:
            worker.join(6)
    assert requests.count("/slow-file") == 1


@pytest.mark.parametrize("cleanup_fault", [False, True])
def test_model_interrupt_after_browser_use_reclaims_idle_resource(
    tmp_path, lifecycle_site, monkeypatch, cleanup_fault
):
    origin, requests, _, *_ = lifecycle_site
    closed = []
    original = ChromiumDriver.close

    def close(driver):
        closed.append(driver)
        original(driver)
        if cleanup_fault:
            raise BrowserError("cleanup_failed", "injected after real close")

    monkeypatch.setattr(ChromiumDriver, "close", close)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        raise RequestInterrupted()

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        _,
    ):
        with pytest.raises(RequestInterrupted) as error:
            app.send("interrupt model")
        assert len(closed) == 1
        if cleanup_fault:
            assert any("清理失败" in note for note in error.value.__notes__)
    assert requests.count("/") == len(closed) == 1


def test_download_replays_reference_without_browser_or_second_request(
    tmp_path, lifecycle_site, monkeypatch
):
    origin, requests, _, *_ = lifecycle_site

    def respond(body, number):
        if number == 1:
            result = tool_response([call("open", "browser_open", url=origin)])
        elif number == 2:
            result = tool_response([current_control(body, "Download attachment", download=True)])
        else:
            assert outputs(body)[-1]["status"] == "downloaded"
            result = answer("download recorded")
        import httpx

        return httpx.Response(200, json=result)

    with captured_app(
        tmp_path, respond, config=configured(tmp_path, origin), confirm_browser=lambda _: True
    ) as (app, recorder, _, source, _):
        assert recorder.run(lambda: app.send("task")) == "download recorded"
    original = source.read_bytes()
    forbid_external_actions(monkeypatch)
    monkeypatch.setattr(
        ChromiumDriver,
        "__init__",
        lambda *_a, **_k: pytest.fail("offline replay launched Chromium"),
    )
    result = replay_turn(source, tmp_path / "offline")
    assert result["status"] == "matched", result
    assert source.read_bytes() == original and requests.count("/file") == 1


@pytest.mark.parametrize("label", ["Open child", "Fragment child"])
def test_tab_switch_round_trip_never_revives_old_controls(lifecycle_site, label):
    origin, requests, _, *_ = lifecycle_site
    token = CancellationToken()
    with open_browser(BrowserPolicy(frozenset({origin})), channel="chrome") as browser:
        first = browser.open(origin)
        link = next(item for item in first.elements if item.name == label)
        browser.perform(BrowserAction("click", link.reference))
        tabs = browser.tabs()
        child = next(tab for tab in tabs if not tab.active)
        browser.perform_tab(
            browser.plan_tab("select_tab", first.browser_id, child.tab_id, cancel=token),
            cancel=token,
        )
        observed = browser.observe()
        assert observed.tab_id == child.tab_id and "Child evidence" in observed.text
        browser.perform_tab(
            browser.plan_tab("select_tab", first.browser_id, first.tab_id, cancel=token),
            cancel=token,
        )
        with pytest.raises(BrowserError, match="当前观察"):
            browser.inspect_reference(link.reference)
        with pytest.raises(BrowserError, match="当前浏览器"):
            browser.plan_tab("close_tab", "f" * 32, child.tab_id, cancel=token)
        assert len(browser.tabs()) == 2
    assert requests.count("/child") == 1


def test_tab_navigation_while_approving_does_not_close_changed_page(
    tmp_path, lifecycle_site, monkeypatch
):
    origin, _, _, *_ = lifecycle_site
    drivers = []
    original = ChromiumDriver._start

    async def start(driver, channel):
        await original(driver, channel)
        drivers.append(driver)

    monkeypatch.setattr(ChromiumDriver, "_start", start)

    def approve(plan):
        if plan.operation == "close_tab":
            # 用户等待期间发生真实页面导航；不替换生产批准后复检逻辑。
            drivers[0].navigate(origin + "/child", CancellationToken())
        return True

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            observed = outputs(body)[-1]["observation"]
            return tool_response(
                [
                    call(
                        "close",
                        "browser_close_tab",
                        browser_id=observed["browser_id"],
                        tab_id=observed["tab_id"],
                    )
                ]
            )
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("changed target retained")

    with application(tmp_path, respond, configured(tmp_path, origin), approve) as (app, _, _):
        assert app.send("close original target") == "changed target retained"
        assert len(drivers[0].tabs(CancellationToken())) == 1
        assert drivers[0].tabs(CancellationToken())[0].url == origin + "/child"


@pytest.mark.parametrize(
    "bad_source",
    [{"tool_call_id": "not-this-tool"}, {"turn_id": "invalid"}, {"request_id": "invalid"}],
)
def test_download_source_is_checked_before_approval_or_request(
    tmp_path, lifecycle_site, monkeypatch, bad_source
):
    origin, requests, _, *_ = lifecycle_site
    approvals = []
    original = BrowserTools.bind_source

    def forged(tools, source):
        # 模拟宿主关联错误，不是让模型自行声明来源。
        original(tools, {**source, **bad_source})

    monkeypatch.setattr(BrowserTools, "bind_source", forged)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        return tool_response([current_control(body, "Download attachment", download=True)])

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda plan: approvals.append(plan) or True
    ) as (app, store, _):
        with pytest.raises(StorageError, match="来源"):
            app.send("download")
        assert not (store.root / "_media").exists()
    assert [plan.operation for plan in approvals] == ["open"] and "/file" not in requests


def test_reusing_download_reference_never_downloads_twice(tmp_path, lifecycle_site):
    origin, requests, _, *_ = lifecycle_site
    saved, approvals = [], []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            saved.append(current_control(body, "Download attachment", download=True))
            return tool_response(saved)
        if number == 3:
            assert outputs(body)[-1]["status"] == "downloaded"
            return tool_response([("repeat", saved[0][1], saved[0][2])])
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("already saved, not repeated")

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda plan: approvals.append(plan) or True
    ) as (app, store, _):
        assert app.send("save once") == "already saved, not repeated"
        assert len(list((store.root / "_media").glob("*/*.media"))) == 1
    assert requests.count("/file") == 1 and [plan.operation for plan in approvals] == [
        "open",
        "download",
    ]
    assert approvals[-1].url == origin + "/file"


def test_cross_session_download_and_close_references_are_rejected(tmp_path, lifecycle_site):
    origin, requests, _, *_ = lifecycle_site
    saved = {}

    def respond(body, number):
        if number in {1, 3}:
            return tool_response([call("open" + str(number), "browser_open", url=origin)])
        if number == 2:
            saved["download"] = current_control(body, "Download attachment", download=True)
            saved["browser"] = outputs(body)[-1]["observation"]["browser_id"]
            return answer("first owner")
        if number == 4:
            return tool_response([saved["download"]])
        if number == 5:
            assert outputs(body)[-1]["status"] == "stale_reference"
            return tool_response(
                [call("close-wrong-owner", "browser_close", browser_id=saved["browser"])]
            )
        assert outputs(body)[-1]["status"] == "stale_reference"
        return answer("owner isolated")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        _,
    ):
        assert app.send("first") == "first owner"
        app.new()
        assert app.send("second") == "owner isolated"
    assert "/file" not in requests and requests.count("/") == 2


@pytest.mark.parametrize("termination", ["browser_close", "renderer_crash"])
def test_browser_termination_after_real_post_keeps_dispatch_and_never_resubmits(
    tmp_path, lifecycle_site, monkeypatch, termination
):
    origin, requests, posts, *_ = lifecycle_site
    original = ChromiumDriver.act

    def terminate_after_post(driver, snapshot, action, cancel):
        result = original(driver, snapshot, action, cancel)
        # 等到本地服务器真实接受 POST、页面收到回执，再关闭实际浏览器连接。
        driver._runner.run(
            driver._page.wait_for_function("document.body.dataset.committed === 'yes'")
        )
        if termination == "browser_close":
            driver._runner.run(driver._browser.close())
        else:

            async def crash():
                cdp = await driver._context.new_cdp_session(driver._page)
                try:
                    await asyncio.wait_for(cdp.send("Page.crash"), timeout=2)
                except (PlaywrightError, TimeoutError):
                    pass

            driver._runner.run(crash())
            assert driver._failed, "real renderer crash event must disable the backend"
        return result

    monkeypatch.setattr(ChromiumDriver, "act", terminate_after_post)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Commit once")])
        result = outputs(body)[-1]
        assert result["status"] == "dispatched" and result["observation_unavailable"]
        return answer("dispatch known, result page unavailable")

    config = configured(tmp_path, origin)
    with application(tmp_path, respond, config, lambda _: True) as (app, store, _):
        assert app.send("commit once") == "dispatch known, result page unavailable"
        sid = app.session.session_id
        before = store.load_events(sid)
    with SessionStore(config.sessions_dir) as restored:
        restored.select_session(sid)
        assert restored.load_events(sid) == before

    def after_restart(body, number):
        if number == 1:
            return tool_response([call("observe", "browser_observe")])
        assert outputs(body)[-1]["status"] == "not_open"
        return answer("explicit open required")

    with application(
        tmp_path, after_restart, config, lambda _: pytest.fail("restart must not approve or open")
    ) as (app, _, _):
        app.resume(sid)
        assert app.send("observe only") == "explicit open required"
    assert posts == ["/commit"] and requests.count("/") == 1


def test_regular_click_does_not_inherit_download_storage_approval(tmp_path, lifecycle_site):
    origin, requests, _, *_ = lifecycle_site
    approvals = []

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Download attachment")])
        pytest.fail("unapproved attachment must not trigger another model request")

    with application(
        tmp_path, respond, configured(tmp_path, origin), lambda plan: approvals.append(plan) or True
    ) as (app, store, _):
        with pytest.raises(BrowserActionUnknownError):
            app.send("click only")
        assert not (store.root / "_media").exists()
    assert requests.count("/file") == 1 and [plan.operation for plan in approvals] == [
        "open",
        "click",
    ]


def test_same_origin_download_redirect_is_followed_once_as_get(tmp_path, lifecycle_site):
    origin, requests, posts, *_ = lifecycle_site

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Redirect attachment", download=True)])
        assert outputs(body)[-1]["status"] == "downloaded"
        return answer("redirect bytes verified")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        store,
        _,
    ):
        assert app.send("download through redirect") == "redirect bytes verified"
        ref = next(
            event["payload"]
            for event in store.load_events(app.session.session_id)
            if event["type"] == "media_saved"
        )
        assert (
            SessionMedia(store, MediaStore(store.root / "_media"))
            .read(ref["media_id"], expected_version=ref["version"])
            .data
            == BINARY
        )
    assert requests.count("/file-redirect") == requests.count("/file") == 1 and posts == []


def test_explicit_context_close_failure_stops_round_without_reopen(
    tmp_path, lifecycle_site, monkeypatch
):
    origin, requests, _, *_ = lifecycle_site
    original = ChromiumDriver.close

    def close(driver):
        original(driver)
        raise BrowserError("cleanup_failed", "injected confirmation lost after real close")

    monkeypatch.setattr(ChromiumDriver, "close", close)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response(
                [
                    call(
                        "close",
                        "browser_close",
                        browser_id=outputs(body)[-1]["observation"]["browser_id"],
                    )
                ]
            )
        pytest.fail("uncertain close must stop before another model request")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        bodies,
    ):
        with pytest.raises(BrowserActionUnknownError, match="关闭未确认"):
            app.send("close once")
        assert len(bodies) == 2
    assert requests.count("/") == 1


@pytest.mark.parametrize("delay", [0, 0.2])
def test_popup_request_delivery_waits_within_approved_action(
    tmp_path, lifecycle_site, monkeypatch, delay
):
    origin, requests, _, *_ = lifecycle_site
    original = ChromiumDriver._route

    async def delayed(driver, route):
        if route.request.url == origin + "/child":
            # 延后真实 SDK 路由回调，不替换网络请求或生产生命周期。
            await asyncio.sleep(delay)
        await original(driver, route)

    monkeypatch.setattr(ChromiumDriver, "_route", delayed)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Open child")])
        if number == 3:
            return tool_response([call("tabs", "browser_tabs")])
        assert len(outputs(body)[-1]["tabs"]) == 2
        return answer("popup delivery synchronized")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        _,
        _,
    ):
        assert app.send("popup") == "popup delivery synchronized"
    assert requests.count("/child") == 1


def test_tab_loss_before_download_redirect_never_opens_replacement(
    tmp_path, lifecycle_site, monkeypatch
):
    origin, requests, _, *_ = lifecycle_site
    original = ChromiumDriver._navigate
    lost = []

    async def lose_tab(driver, url):
        if driver._acting:
            # 关闭真实页面，模拟原响应已确认而重定向尚未派发的资源丢失边界。
            assert driver._page is not None
            await driver._page.close(run_before_unload=False)
            lost.append(url)
        return await original(driver, url)

    monkeypatch.setattr(ChromiumDriver, "_navigate", lose_tab)

    def respond(body, number):
        if number == 1:
            return tool_response([call("open", "browser_open", url=origin)])
        if number == 2:
            return tool_response([current_control(body, "Redirect attachment", download=True)])
        pytest.fail("resource loss must stop without another model request")

    with application(tmp_path, respond, configured(tmp_path, origin), lambda _: True) as (
        app,
        store,
        bodies,
    ):
        with pytest.raises(BrowserActionUnknownError):
            app.send("download once")
        assert len(bodies) == 2 and not (store.root / "_media").exists()
    assert lost == [origin + "/file"]
    assert requests.count("/") == requests.count("/file-redirect") == 1
    assert "/file" not in requests
