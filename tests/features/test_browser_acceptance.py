"""C02.4：仅替换模型 HTTP；真实查询、Chrome、一次 POST、取消和独立落盘核验。"""

import json

import pytest

from adam.browser.adapters.chromium import ChromiumDriver
from adam.contracts.errors import RequestInterrupted
from adam.llm.http_transport import InterruptibleTransport
from scripts import browser_acceptance as evaluation
from scripts.browser_fixture import registry_site
from tests.features.test_streaming import DONE, USAGE, event, sse
from tests.features.test_subagents import call, outputs


def wire(calls=None, text=None):
    delta = {"role": "assistant", "content": text or ""}
    if calls:
        delta["tool_calls"] = [
            {
                "index": index,
                "id": identity,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            for index, (identity, name, args) in enumerate(calls)
        ]
    return sse([event(delta), event(reason="tool_calls" if calls else "stop", usage=USAGE), DONE])


def current_action(body, identity, label, operation="click", **values):
    """模拟模型仅从实际收到的最近有效观察取引用，不读 Driver 或页面私有对象。"""
    observation = next(r["observation"] for r in reversed(outputs(body)) if "observation" in r)
    element = next(e for e in observation["elements"] if e["name"] == label)
    return call(identity, "browser_action", **element["reference"], action=operation, **values)


@pytest.mark.parametrize(
    "boundary", ["normal", "after_navigation", "after_observe", "repeat_submit", "cancel_submit"]
)
def test_query_reserve_receipt_and_failure_boundaries(tmp_path, monkeypatch, boundary):
    """旧引用/重复由模型 HTTP 注入，取消发生在批准回调；站点与浏览器实际运行。"""
    bodies, closed, saved = [], [], {}
    original_close = ChromiumDriver.close

    def close(driver):
        closed.append(driver)
        return original_close(driver)

    monkeypatch.setattr(ChromiumDriver, "close", close)
    with registry_site(tmp_path / "site") as (origin, state):
        sequence = ["discover", "open", "query", "find"]
        if boundary == "after_navigation":
            sequence.append("old_find")
        if boundary == "after_observe":
            sequence.extend(["observe", "old_link"])
        sequence.extend(["reserve", "operator", "code", "save"])
        if boundary == "repeat_submit":
            sequence.append("old_save")
        sequence.append("answer")

        def handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            step = sequence[len(bodies) - 1]
            if saved.pop("expect_stale", False):
                assert outputs(body)[-1]["status"] == "stale_reference"
            if step == "discover":
                return wire(
                    [
                        call(
                            "discover",
                            "discover_tools",
                            names=["browser_open", "browser_action", "browser_observe"],
                        )
                    ]
                )
            if step == "open":
                return wire([call("open", "browser_open", url=origin)])
            if step == "query":
                return wire([current_action(body, "query", "Record search", "fill", value="Orion")])
            if step == "find":
                saved["find"] = current_action(body, "find", "Find record")
                return wire([saved["find"]])
            if step == "observe":
                saved["link"] = current_action(body, "link", "Reserve Orion")
                return wire([call("observe", "browser_observe")])
            if step.startswith("old_"):
                original = saved[step.removeprefix("old_")]
                saved["expect_stale"] = True
                return wire([("stale-" + step, original[1], original[2])])
            if step == "reserve":
                assert "O-731" in outputs(body)[-1].get("observation", {}).get(
                    "text", ""
                ) or boundary.startswith("after_")
                return wire([current_action(body, "reserve", "Reserve Orion")])
            if step == "operator":
                return wire(
                    [current_action(body, "operator", "Operator", "fill", value="Eval User")]
                )
            if step == "code":
                return wire(
                    [current_action(body, "code", "Verification code", "fill", value="O-731")]
                )
            if step == "save":
                saved["save"] = current_action(body, "save", "Save reservation")
                return wire([saved["save"]])
            assert step == "answer"
            receipt = next(r["observation"] for r in reversed(outputs(body)) if "observation" in r)
            assert receipt["url"] == origin + "/receipt/R0001" and "Eval User" in receipt["text"]
            return wire(text="Receipt R0001: Orion, O-731, Eval User.")

        monkeypatch.setattr(
            InterruptibleTransport, "handle_request", lambda _, request: handle(request)
        )
        if boundary == "cancel_submit":
            build = evaluation.build_application

            def interrupted(*args, **kwargs):
                confirm = kwargs["confirm_browser"]

                def approve(plan):
                    if plan.operation == "click" and plan.name == "Save reservation":
                        raise RequestInterrupted()
                    return confirm(plan)

                kwargs["confirm_browser"] = approve
                return build(*args, **kwargs)

            monkeypatch.setattr(evaluation, "build_application", interrupted)
        item = evaluation.execute(tmp_path, origin, 8087, "test")
    observation = evaluation.observables(tmp_path, state)
    logs = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert len(closed) == 1 and state["queries"] == ["Orion"]
    assert bodies[0]["stream"]
    assert len(item["approvals"]) == (6 if boundary == "cancel_submit" else 7)
    if boundary == "cancel_submit":
        assert not item["turn_success"] and item["error"]["type"] == "RequestInterrupted"
        assert state["post_attempts"] == [] and observation["persisted_receipts"] == []
        assert not observation["actual_form_completion"] and len(bodies) == 8
        assert item["diagnostics"][0]["turns"][0]["status"] == "interrupted"
    else:
        assert item["turn_success"] and observation["actual_form_completion"]
        assert item["answer"] == "Receipt R0001: Orion, O-731, Eval User."
        assert len(state["post_attempts"]) == 1 and len(observation["persisted_receipts"]) == 1
        assert any(r["path"] == "/receipt/R0001" for r in state["requests"])
        if boundary != "normal":
            assert any(
                e["type"] == "tool_finished" and "stale_reference" in e["payload"].get("output", "")
                for e in logs
            )
        assert observation["answer_quality"] == "requires_independent_semantic_review"
    assert item["cost"] is None and not item["diagnostics"][0]["unknown_usage"]
    before = len(bodies)
    with pytest.raises(FileExistsError):
        evaluation.execute(tmp_path, origin, 8087, "test")
    assert len(bodies) == before
