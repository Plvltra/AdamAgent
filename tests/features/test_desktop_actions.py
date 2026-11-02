"""C03.2 经原模型工具循环验证批准、版本和动作收尾；仅桌面端口使用合成帧。"""

from dataclasses import replace

import pytest

from adam.config.settings import ToolDiscoveryConfig
from adam.contracts.desktop import DesktopActionUnknownError
from adam.contracts.errors import ApprovalDeniedError
from adam.desktop.models import Control
from tests.features.test_browser_actions import application
from tests.features.test_desktop_observation import Backend
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_discovery_loop import settings
from tests.features.test_tool_loop import answer, tool_response


class Actions(Backend):
    def __init__(self):
        super().__init__()
        self.frame = replace(
            self.frame,
            controls=(Control(None, (1, 2), "Field", "Edit", "field", (100, 200, 400, 500), True),),
        )
        self.actions = []
        self.unknown = False

    def perform(self, plan, expected, cancel):
        cancel.check()
        self.actions.append(plan)
        if self.unknown:
            raise DesktopActionUnknownError("unknown dispatch")
        self.frame = replace(self.frame, title="changed after action")


def dialogue(operation="fill", **values):
    def respond(body, number):
        if number == 1:
            return tool_response([call("observe", "desktop_observe")])
        if number == 2:
            return tool_response(
                [
                    call(
                        "action",
                        "desktop_action",
                        version=outputs(body)[-1]["version"],
                        operation=operation,
                        **values,
                    )
                ]
            )
        return answer("done")

    return respond


def config(root):
    return settings(root, tool_discovery=ToolDiscoveryConfig(mode="full"))


@pytest.mark.parametrize(
    "operation,values",
    [
        ("fill", {"control": 0, "value": "literal {ENTER}"}),
        ("click", {"x": 1, "y": 1}),
        ("key", {"value": "TAB"}),
    ],
)
def test_actions_use_original_approval_source_and_fresh_observation(tmp_path, operation, values):
    backend, approvals = Actions(), []
    with application(
        tmp_path,
        dialogue(operation, **values),
        config(tmp_path),
        desktop_backend=backend,
        confirm_desktop=lambda plan: approvals.append(plan) or True,
    ) as (app, store, bodies):
        app.select_mode("act")
        app.send("test")
        result = outputs(bodies[-1])[-1]
        assert result["status"] == "dispatched"
        assert len(backend.actions) == len(approvals) == 1
        assert backend.actions[0] == approvals[0]
        assert result["version"] != outputs(bodies[1])[-1]["version"]
        assert result["image"]["source"]["tool_name"] == "desktop_action"
        events = store.load_events(app.session.session_id)
        assert len([e for e in events if e["type"] == "media_saved"]) == 2
        if operation == "click":
            assert approvals[0].point == (250, 350)


@pytest.mark.parametrize("boundary", ["denied", "changed", "plan"])
def test_refused_or_changed_window_never_dispatches(tmp_path, boundary):
    backend, approvals = Actions(), []

    def approve(plan):
        approvals.append(plan)
        if boundary == "changed":
            backend.frame = replace(backend.frame, title="external change")
        return boundary != "denied"

    with application(
        tmp_path,
        dialogue(control=0, value="new"),
        config(tmp_path),
        desktop_backend=backend,
        confirm_desktop=approve,
    ) as (app, store, bodies):
        app.select_mode("plan" if boundary == "plan" else "act")
        if boundary == "denied":
            with pytest.raises(ApprovalDeniedError):
                app.send("test")
        else:
            app.send("test")
            assert outputs(bodies[-1])[-1]["ok"] is False
        assert backend.actions == []
        assert len(approvals) == (0 if boundary == "plan" else 1)
        assert (
            len(
                [e for e in store.load_events(app.session.session_id) if e["type"] == "media_saved"]
            )
            == 1
        )


def test_unknown_native_result_stops_instead_of_retrying(tmp_path):
    backend = Actions()
    backend.unknown = True
    with application(
        tmp_path,
        dialogue(control=0, value="new"),
        config(tmp_path),
        desktop_backend=backend,
        confirm_desktop=lambda plan: True,
    ) as (app, store, bodies):
        app.select_mode("act")
        with pytest.raises(DesktopActionUnknownError):
            app.send("test")
        assert len(backend.actions) == 1 and len(bodies) == 2


def test_previous_action_reference_cannot_be_reused(tmp_path):
    backend = Actions()
    previous = []

    def respond(body, number):
        if number == 1:
            return tool_response([call("observe", "desktop_observe")])
        if number == 2:
            previous.append(outputs(body)[-1]["version"])
        if number in (2, 3):
            return tool_response(
                [
                    call(
                        str(number),
                        "desktop_action",
                        version=previous[0],
                        operation="key",
                        value="TAB",
                    )
                ]
            )
        return answer("done")

    with application(
        tmp_path,
        respond,
        config(tmp_path),
        desktop_backend=backend,
        confirm_desktop=lambda plan: True,
    ) as (app, _, bodies):
        app.select_mode("act")
        app.send("test")
        assert len(backend.actions) == 1
        assert outputs(bodies[-1])[-1]["ok"] is False
