"""C02.1：真实 Chromium 与本地动态站点；不截图、不请求模型或用户配置。"""

import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adam.browser.models import BrowserError, ObservationLimits
from adam.browser.policy import BrowserPolicy
from adam.browser.runtime import open_browser
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted

HTML = b"""<!doctype html><html><head><title>Records</title></head><body>
<h1>Local records</h1><p id="status">Loading</p>
<label for="query">Record query</label><input id="query" value="private-input-value">
<input type="password" value="private-password-value">
<button id="target">Find record</button><select aria-label="Category"><option>All</option></select>
<button hidden>Hidden control</button><button style="visibility:hidden">Invisible control</button>
<button disabled>Disabled control</button><fieldset disabled><button>Disabled by fieldset</button></fieldset>
<div aria-disabled="true"><button>Disabled by ancestor</button></div>
<div inert><button>Inert control</button></div><span style="display:none">HIDDEN_TEXT</span>
<script>
let version = -1;
async function update() {
  const state = await (await fetch('/state')).json();
  if (state.version === version) return;
  version = state.version;
  document.querySelector('#status').textContent = 'Loaded record ' + version;
  if (state.mode === 'replace') document.querySelector('#target').outerHTML = '<button id="target">Find record</button>';
  if (state.mode === 'disable') document.querySelector('#target').disabled = true;
  if (state.mode === 'property') document.querySelector('#query').value = 'changed-without-dom-mutation';
  if (state.mode === 'navigate') { location.href = '/next'; return; }
  if (state.mode === 'text') document.querySelector('#status').textContent = 'Updated record';
  fetch('/ack?version=' + version);
}
update(); setInterval(update, 50);
</script></body></html>"""


@pytest.fixture
def browser_site():
    """状态注入只控制测试网页的异步变化；真实页面执行 JS 后发送 ack。"""
    state = {"version": 0, "mode": "initial"}
    ack, slow_started, release = threading.Event(), threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            status, headers, body = 200, {}, HTML
            if self.path == "/state":
                headers["Content-Type"] = "application/json"
                body = json.dumps(state).encode()
            elif self.path.startswith("/ack"):
                ack.set()
                body = b"ok"
            elif self.path == "/unicode-invalid":
                body = b"<body><script>document.body.textContent = 'A\\ud800B';</script>"
            elif self.path == "/unicode":
                body = b"<h1>A&#128512;B</h1>"
            elif self.path == "/next":
                body = b"<title>Next</title><h1>Next page</h1><button>Next control</button>"
            elif self.path == "/error":
                status, body = 503, b"private-server-error"
            elif self.path == "/redirect":
                status, headers, body = 302, {"Location": "/next"}, b""
            elif self.path == "/loop":
                status, headers, body = 302, {"Location": "/loop"}, b""
            elif self.path == "/resource-redirect":
                status, headers, body = 302, {"Location": "/resource-leak"}, b""
            elif self.path == "/redirect-chain":
                status, headers, body = 302, {"Location": "/denied"}, b""
            elif self.path == "/safe-resource-redirect":
                body = b'<h1>Resource redirect</h1><script>fetch("/resource-redirect").catch(()=>{}); fetch("/submit",{method:"POST",body:"blocked"}).catch(()=>{});</script>'
            elif self.path == "/denied":
                status, headers, body = (
                    302,
                    {"Location": f"http://localhost:{server.server_port}/leak"},
                    b"",
                )
            elif self.path == "/external":
                body = f'<h1>Safe page</h1><script>fetch("http://localhost:{server.server_port}/leak").catch(()=>{{}}); new WebSocket("ws://127.0.0.1:{server.server_port}/socket");</script>'.encode()
            elif self.path.startswith("/cookie"):
                body = b"<h1>Isolated cookies</h1>"
                if self.path == "/cookie-set":
                    headers["Set-Cookie"] = "fixture_session=local; Path=/"
            self.send_response(status)
            self.send_header(
                "Content-Type", headers.pop("Content-Type", "text/html; charset=utf-8")
            )
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            if self.path == "/slow":
                slow_started.set()
                release.wait(10)
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield origin, state, ack, requests, slow_started, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)


def policy_at(site):
    return BrowserPolicy(frozenset({site[0]}))


def loaded_page(browser, site):
    browser.open(site[0])
    deadline = time.monotonic() + 4
    page = browser.observe()
    while not site[2].is_set() and time.monotonic() < deadline:
        time.sleep(0.03)
        page = browser.observe()
    assert site[2].is_set(), "real page did not execute dynamic JavaScript"
    return page


def find_reference(observation, name):
    return next(item.reference for item in observation.elements if item.name == name)


def test_real_dynamic_dom_has_bounded_provenance_and_only_available_controls(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        page = loaded_page(browser, browser_site)
        reference = find_reference(page, "Find record")
        assert browser.inspect_reference(reference).name == "Find record"
    assert page.schema_version == 1 and page.url == browser_site[0] + "/"
    assert page.title == "Records" and "Loaded record 0" in page.text
    assert page.browser_id and page.tab_id and page.document_id and page.observation_id
    assert datetime.fromisoformat(page.observed_at).tzinfo is not None
    assert not page.text_truncated and not page.elements_truncated
    names = {item.name for item in page.elements}
    assert {"Find record", "Record query", "Category"} <= names
    assert (
        not {
            "Hidden control",
            "Invisible control",
            "Disabled control",
            "Disabled by fieldset",
            "Disabled by ancestor",
            "Inert control",
        }
        & names
    )
    serialized = json.dumps(asdict(page))
    assert "private-input-value" not in serialized and "private-password-value" not in serialized
    assert "HIDDEN_TEXT" not in page.text and "setInterval" not in page.text


@pytest.mark.parametrize("mode", ["replace", "disable", "text", "property"])
def test_dynamic_change_invalidates_old_reference_and_reobservation_recovers(browser_site, mode):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        old = loaded_page(browser, browser_site)
        name = "Record query" if mode == "property" else "Find record"
        reference = find_reference(old, name)
        browser_site[2].clear()
        browser_site[1].update(version=1, mode=mode)
        deadline = time.monotonic() + 4
        failure = None
        while time.monotonic() < deadline:
            try:
                browser.inspect_reference(reference)
            except BrowserError as error:
                failure = error
                break
            time.sleep(0.03)
        assert failure is not None and failure.kind == "stale_reference"
        assert "重新观察" in str(failure)
        new = browser.observe()
        assert new.browser_id == old.browser_id and new.tab_id == old.tab_id
        assert new.document_id == old.document_id and new.observation_id != old.observation_id
        if mode == "disable":
            assert "Find record" not in {item.name for item in new.elements}
        else:
            fresh = find_reference(new, name)
            assert fresh != reference and browser.inspect_reference(fresh).reference == fresh


def test_script_property_change_without_mutation_is_detected(browser_site):
    """故障注入网页属性边界：不触发 input 事件或 MutationObserver，仍拒绝旧控件。"""
    from adam.browser.adapters.chromium import ChromiumDriver
    from adam.browser.service import BrowserObserver

    driver = ChromiumDriver(policy_at(browser_site), ObservationLimits(), channel="chrome")
    try:
        browser = BrowserObserver(driver, policy_at(browser_site))
        old = loaded_page(browser, browser_site)
        assert driver._page is not None
        driver._runner.run(
            driver._page.evaluate("document.querySelector('#query').value = 'property-only'")
        )
        with pytest.raises(BrowserError) as caught:
            browser.inspect_reference(find_reference(old, "Record query"))
        assert caught.value.kind == "stale_reference"
    finally:
        driver.close()


def test_navigation_changes_document_but_not_tab_identity(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        old = loaded_page(browser, browser_site)
        new = browser.open(browser_site[0] + "/redirect")
        assert new.tab_id == old.tab_id and new.browser_id == old.browser_id
        assert new.document_id != old.document_id and new.url.endswith("/next")
        with pytest.raises(BrowserError) as caught:
            browser.inspect_reference(find_reference(old, "Find record"))
        assert caught.value.kind == "stale_reference"
        assert browser.inspect_reference(find_reference(new, "Next control")).tag == "button"


def test_cross_observer_forged_and_superseded_references_are_rejected(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as first:
        page = loaded_page(first, browser_site)
        reference = find_reference(page, "Find record")
        with open_browser(policy_at(browser_site), channel="chrome") as second:
            other = loaded_page(second, browser_site)
            assert other.browser_id != page.browser_id and other.tab_id != page.tab_id
            with pytest.raises(BrowserError) as caught:
                second.inspect_reference(reference)
            assert caught.value.kind == "stale_reference"
        with pytest.raises(BrowserError):
            first.inspect_reference(replace(reference, element_id="forged"))
        first.observe()
        with pytest.raises(BrowserError):
            first.inspect_reference(reference)


def test_truncation_keeps_identity_and_only_returns_referenced_subset(browser_site):
    limits = ObservationLimits(max_chars=7, max_elements=1)
    with open_browser(policy_at(browser_site), limits, channel="chrome") as browser:
        page = loaded_page(browser, browser_site)
        assert page.text == "Local r" and page.text_truncated and page.elements_truncated
        assert len(page.elements) == 1 and page.url == browser_site[0] + "/"
        assert browser.inspect_reference(page.elements[0].reference) == page.elements[0]


def test_redirect_and_subresource_cannot_expand_origin_allowlist(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        page = browser.open(browser_site[0] + "/external")
        deadline = time.monotonic() + 3
        while page.blocked_requests < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            page = browser.observe()
        assert page.blocked_requests >= 2 and "Safe page" in page.text
        with pytest.raises(BrowserError) as caught:
            browser.open(browser_site[0] + "/denied")
        assert caught.value.kind == "network_denied"
    paths = [item[0] for item in browser_site[3]]
    assert "/leak" not in paths and "/socket" not in paths
    assert paths.count("/denied") == 1


def test_context_cookie_state_is_never_shared_between_browser_owners(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as first:
        first.open(browser_site[0] + "/cookie-set")
        first.open(browser_site[0] + "/cookie")
    with open_browser(policy_at(browser_site), channel="chrome") as second:
        second.open(browser_site[0] + "/cookie")
    cookies = [
        headers.get("cookie", headers.get("Cookie", ""))
        for path, headers in browser_site[3]
        if path == "/cookie"
    ]
    assert cookies == ["fixture_session=local", ""]


def test_http_failure_does_not_publish_snapshot_or_raw_server_body(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        old = loaded_page(browser, browser_site)
        with pytest.raises(BrowserError) as caught:
            browser.open(browser_site[0] + "/error")
        assert caught.value.kind == "http_error" and "private-server-error" not in str(caught.value)
        with pytest.raises(BrowserError):
            browser.inspect_reference(old.elements[0].reference)
    assert [path for path, _ in browser_site[3]].count("/error") == 1


def test_inflight_cancel_closes_actual_browser_without_reissuing_request(browser_site, monkeypatch):
    from adam.browser.adapters.chromium import ChromiumDriver

    observed, retained = [], []
    original = ChromiumDriver.close

    def close(driver):
        retained.append(driver._browser)
        original(driver)

    monkeypatch.setattr(ChromiumDriver, "close", close)
    cancel = CancellationToken()

    def run():
        try:
            with open_browser(policy_at(browser_site), channel="chrome") as browser:
                browser.open(browser_site[0] + "/slow", cancel=cancel)
        except RequestInterrupted:
            observed.append("cancelled")

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert browser_site[4].wait(6)
    cancel.cancel()
    worker.join(40)
    assert not worker.is_alive() and observed == ["cancelled"]
    assert retained and all(item is not None and not item.is_connected() for item in retained)
    assert [path for path, _ in browser_site[3]].count("/slow") == 1


def test_already_cancelled_does_not_create_sdk_or_send_requests(browser_site, monkeypatch):
    from adam.browser.adapters.chromium import ChromiumDriver

    def unexpected(*args, **kwargs):
        raise AssertionError("cancelled request launched a browser")

    monkeypatch.setattr(ChromiumDriver, "__init__", unexpected)
    cancel = CancellationToken()
    cancel.cancel()
    with pytest.raises(RequestInterrupted), open_browser(policy_at(browser_site), cancel=cancel):
        pass
    assert browser_site[3] == []


def test_observation_deadline_disables_backend_and_close_disconnects_engine(browser_site):
    from adam.browser.adapters.chromium import ChromiumDriver

    driver = ChromiumDriver(
        policy_at(browser_site), ObservationLimits(timeout_seconds=8), channel="chrome"
    )
    engine = driver._browser
    try:
        with pytest.raises(BrowserError) as caught:
            driver.navigate(browser_site[0] + "/slow", CancellationToken())
        assert caught.value.kind == "timeout"
        with pytest.raises(BrowserError) as closed:
            driver.snapshot(CancellationToken())
        assert closed.value.kind == "closed"
    finally:
        driver.close()
        driver.close()
    assert engine is not None and not engine.is_connected()
    assert [path for path, _ in browser_site[3]].count("/slow") == 1


def test_standalone_cli_returns_real_dom_json_and_no_model_session(browser_site):
    result = subprocess.run(
        [sys.executable, "-m", "adam.browser", browser_site[0] + "/next", "--channel", "chrome"],
        capture_output=True,
        text=True,
        timeout=75,
    )
    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    assert page["ok"] and page["title"] == "Next" and page["schema_version"] == 1
    assert page["elements"][0]["name"] == "Next control"
    assert page["elements"][0]["reference"]["observation_id"] == page["observation_id"]


@pytest.mark.parametrize(
    "path,kind", [("/loop", "redirect_limit"), ("/redirect-chain", "network_denied")]
)
def test_redirect_chains_are_checked_before_next_network_hop(browser_site, path, kind):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        with pytest.raises(BrowserError) as caught:
            browser.open(browser_site[0] + path)
        assert caught.value.kind == kind
    paths = [item[0] for item in browser_site[3]]
    assert paths.count(path) == 1 and "/leak" not in paths


def test_subresource_redirect_and_unsolicited_post_do_not_reach_target(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        page = browser.open(browser_site[0] + "/safe-resource-redirect")
        deadline = time.monotonic() + 3
        while page.blocked_requests < 2 and time.monotonic() < deadline:
            time.sleep(0.03)
            page = browser.observe()
        assert page.blocked_requests >= 2
    paths = [item[0] for item in browser_site[3]]
    assert (
        "/resource-redirect" in paths and "/resource-leak" not in paths and "/submit" not in paths
    )


def test_unicode_truncation_does_not_emit_half_surrogate(browser_site):
    with open_browser(
        policy_at(browser_site), ObservationLimits(max_chars=2), channel="chrome"
    ) as browser:
        page = browser.open(browser_site[0] + "/unicode")
        assert page.text == "A" and page.text_truncated
        json.dumps(asdict(page), ensure_ascii=False).encode("utf-8", errors="strict")


def test_page_script_invalid_surrogate_is_normalized_before_json_output(browser_site):
    with open_browser(policy_at(browser_site), channel="chrome") as browser:
        page = browser.open(browser_site[0] + "/unicode-invalid")
        assert page.text == "A\ufffdB" and not page.text_truncated
        json.dumps(asdict(page), ensure_ascii=False).encode("utf-8", errors="strict")
