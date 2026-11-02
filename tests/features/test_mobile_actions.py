"""Android动作通过原模型工具管线；仅ADB替身，来源/模式/批准/存储实际执行。"""

import pytest

from adam.contracts.errors import ApprovalDeniedError
from adam.contracts.mobile import MobileActionUnknownError
from tests.features.test_browser_actions import application
from tests.features.test_desktop_actions import config
from tests.features.test_mobile_observation import Device
from tests.features.test_subagents import call, outputs
from tests.features.test_tool_loop import answer, tool_response


class Actions(Device):
    """替换固定ADB输入边界，记录派发及可注入断连；不生成真实截图。"""

    def __init__(self, root):
        super().__init__(root)
        self.xml = (
            b'<hierarchy rotation="0"><node content-desc="record-name" '
            b'package="com.adam.practice" enabled="true" focused="true" '
            b'class="android.widget.EditText" bounds="[0,0][2,2]" /></hierarchy>'
        )
        self.actions = []
        self.disconnect_after = False

    def _run(self, args, cancel, **kwargs):
        if args[:2] == ["shell", "input"]:
            cancel.check()
            self.actions.append(args)
            if self.disconnect_after:
                self.online = False
            return b""
        return super()._run(args, cancel, **kwargs)


def dialogue(operation, values, *, repeat=False):
    original = None

    def respond(body, number):
        nonlocal original
        if number == 1:
            return tool_response([call("observe", "mobile_observe")])
        if number == 2:
            original = outputs(body)[-1]["version"]
        if number == 2 or (repeat and number == 3):
            return tool_response(
                [
                    call(
                        "action" + str(number),
                        "mobile_action",
                        version=original,
                        operation=operation,
                        **values,
                    )
                ]
            )
        return answer("done")

    return respond


@pytest.mark.parametrize(
    "operation,values",
    [
        ("click", {"control": "record-name"}),
        ("input", {"value": "Record 102"}),
        ("swipe", {"x": 0, "y": 0, "x2": 1, "y2": 1}),
        ("back", {}),
    ],
)
def test_actions_have_original_source_approval_and_single_use_version(tmp_path, operation, values):
    backend, approvals = Actions(tmp_path), []
    with application(
        tmp_path,
        dialogue(operation, values, repeat=True),
        config(tmp_path),
        mobile_observer=backend,
        confirm_mobile=lambda plan: approvals.append(plan) or True,
    ) as (app, store, bodies):
        app.select_mode("act")
        app.send("authorized emulator test")
        assert len(approvals) == len(backend.actions) == 1
        assert outputs(bodies[2])[-1]["status"] == "dispatched"
        assert outputs(bodies[2])[-1]["image"]["source"]["tool_name"] == "mobile_action"
        assert not outputs(bodies[3])[-1]["ok"]
        assert app.session is not None
        assert (
            len(
                [e for e in store.load_events(app.session.session_id) if e["type"] == "media_saved"]
            )
            == 2
        )


@pytest.mark.parametrize("boundary", ["denied", "changed", "disconnected", "plan", "unsafe_text"])
def test_unauthorized_stale_or_invalid_input_is_never_dispatched(tmp_path, boundary):
    backend, approvals = Actions(tmp_path), []

    def approve(plan):
        approvals.append(plan)
        if boundary == "changed":
            backend.rotation = 1
        if boundary == "disconnected":
            backend.online = False
        return boundary != "denied"

    values = {"value": "a;touch bad" if boundary == "unsafe_text" else "text"}
    with application(
        tmp_path,
        dialogue("input", values),
        config(tmp_path),
        mobile_observer=backend,
        confirm_mobile=approve,
    ) as (app, _, bodies):
        app.select_mode("plan" if boundary == "plan" else "act")
        if boundary == "denied":
            with pytest.raises(ApprovalDeniedError):
                app.send("test")
        else:
            app.send("test")
            assert not outputs(bodies[-1])[-1]["ok"]
        assert not backend.actions
        if boundary in {"plan", "unsafe_text"}:
            assert not approvals


def test_disconnect_after_dispatch_stops_round_without_second_action(tmp_path):
    backend = Actions(tmp_path)
    backend.disconnect_after = True
    with application(
        tmp_path,
        dialogue("back", {}),
        config(tmp_path),
        mobile_observer=backend,
        confirm_mobile=lambda _: True,
    ) as (app, store, bodies):
        app.select_mode("act")
        with pytest.raises(MobileActionUnknownError):
            app.send("test")
        assert len(backend.actions) == 1 and len(bodies) == 2
        assert app.session is not None
        assert store.load_turns(app.session.session_id) == []
