"""工具消息的结构与完整轮次校验，供传输层和存储层复用。"""

from typing import Any


def validate_assistant(message: dict[str, Any]) -> None:
    """校验助手消息结构，无效时抛出 ValueError；不修改输入。

    允许普通回答和工具调用请求；工具参数的业务校验由执行层负责。
    """
    if message.get("role") != "assistant":
        raise ValueError("expected assistant")
    content = message.get("content")
    calls = message.get("tool_calls")
    if "reasoning_content" in message and not isinstance(message["reasoning_content"], str):
        raise ValueError("invalid reasoning")
    if calls:
        if not isinstance(calls, list) or content is not None and not isinstance(content, str):
            raise ValueError("invalid calls")
        ids: set[str] = set()
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("invalid call")
            call_id = call.get("id")
            function = call.get("function")
            if not isinstance(call_id, str) or not call_id or call_id in ids:
                raise ValueError("invalid call id")
            ids.add(call_id)
            if (
                not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"]
                or not isinstance(function.get("arguments"), str)
            ):
                raise ValueError("invalid function")
    elif not isinstance(content, str) or not content.strip():
        raise ValueError("empty answer")


def validate_turn(messages: list[dict[str, Any]], *, allow_partial: bool = False) -> None:
    """校验完整轮次，无效时抛出 ValueError；不修改输入。

    要求以用户消息开始、以最终回答结束，工具结果按调用顺序完整配对，
    同一轮次内的工具调用 ID 不重复。allow_partial 仅供中断恢复校验尚未提交的前缀，
    允许缺少末尾回答或工具结果，但不放松已有消息的顺序和结构要求。
    """
    if (
        len(messages) < (1 if allow_partial else 2)
        or messages[0].get("role") != "user"
        or not isinstance(messages[0].get("content"), str)
    ):
        raise ValueError("missing user")
    pending: list[str] = []
    seen: set[str] = set()
    final = False
    for message in messages[1:]:
        if final:
            raise ValueError("messages after final")
        if pending:
            if (
                message.get("role") != "tool"
                or message.get("tool_call_id") != pending.pop(0)
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError("unmatched tool result")
        else:
            validate_assistant(message)
            calls = message.get("tool_calls") or []
            for call in calls:
                if call["id"] in seen:
                    raise ValueError("duplicate tool id")
                seen.add(call["id"])
                pending.append(call["id"])
            final = not calls
    if (pending or not final) and not allow_partial:
        raise ValueError("unfinished turn")
