"""Android固定输入动作；只派发已批准的有限参数，不接受任意shell。"""

import re
import xml.etree.ElementTree as ET
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.mobile import MobileActionUnknownError, MobileApproval
from adam.mobile.observation import AndroidObserver, MobileFrame


def plan_action(args: dict[str, Any], frame: MobileFrame) -> MobileApproval:
    """批准前校验控件/焦点/坐标；ASCII输入不含shell元字符，其他字符明确不支持。"""
    operation = args["operation"]
    values: tuple[str, ...]
    if operation == "click":
        if set(args) != {"version", "operation", "control"}:
            raise ValueError("click requires exactly one control description")
        nodes = [
            node
            for node in ET.fromstring(frame.hierarchy).iter("node")
            if node.get("content-desc") == args["control"]
            and node.get("enabled") == "true"
            and node.get("package") == "com.adam.practice"
        ]
        if len(nodes) != 1:
            raise ValueError("control is missing or ambiguous")
        bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", nodes[0].get("bounds", ""))
        if not bounds:
            raise ValueError("invalid control bounds")
        left, top, right, bottom = map(int, bounds.groups())
        if not 0 <= left < right <= frame.width or not 0 <= top < bottom <= frame.height:
            raise ValueError("control lies outside screenshot")
        values = ("tap", str((left + right) // 2), str((top + bottom) // 2))
    elif operation == "input":
        if set(args) != {"version", "operation", "value"}:
            raise ValueError("input requires only literal value")
        value = args["value"]
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9 _.\-]{1,200}", value) is None:
            raise ValueError(
                "input supports bounded ASCII letters, digits, space, underscore, dot and hyphen"
            )
        if not any(
            node.get("class") == "android.widget.EditText"
            and node.get("focused") == "true"
            and node.get("enabled") == "true"
            and node.get("package") == "com.adam.practice"
            for node in ET.fromstring(frame.hierarchy).iter("node")
        ):
            raise ValueError("input requires a focused practice text field")
        values = ("text", value.replace(" ", "%s"))
    elif operation == "swipe":
        if set(args) != {"version", "operation", "x", "y", "x2", "y2"}:
            raise ValueError("swipe requires two screenshot points")
        for name, limit in (
            ("x", frame.width),
            ("x2", frame.width),
            ("y", frame.height),
            ("y2", frame.height),
        ):
            if type(args[name]) is not int or not 0 <= args[name] < limit:
                raise ValueError("swipe point outside screenshot")
        values = ("swipe", *(str(args[key]) for key in ("x", "y", "x2", "y2")), "300")
    elif operation == "back" and set(args) == {"version", "operation"}:
        values = ("keyevent", "4")
    else:
        raise ValueError("unsupported mobile action or arguments")
    return MobileApproval(frame.serial, args["version"], operation, values)


def perform(observer: AndroidObserver, plan: MobileApproval, cancel: CancellationToken) -> None:
    """宿主在设备租约内完成批准和复检后调用；派发起就按未知处理失败，禁止补发。"""
    cancel.check()
    if plan.serial != observer.serial:
        raise ValueError("mobile approval belongs to another device")
    try:
        observer._run(["shell", "input", *plan.arguments], cancel)
    except BaseException as error:
        raise MobileActionUnknownError("ADB input may have executed; do not retry") from error
