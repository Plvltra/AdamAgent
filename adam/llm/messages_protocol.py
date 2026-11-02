"""在原会话字典与 Messages 文本/函数协议之间转换；不执行或重放工具。"""

import json
from typing import Any

from adam.contracts.model_history import message_view
from adam.contracts.protocol import validate_assistant


def encode_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """返回system/messages字段；保留工具ID，不可移植扩展或未配对结果抛ValueError。

    system仅接受历史开头；不把中途系统约束移动到开头以免改变含义。
    同一批工具结果合并为紧随助手的user消息，不改变结果顺序。
    """
    system: list[str] = []
    output: list[dict[str, Any]] = []
    pending: list[str] = []
    results: list[dict[str, Any]] = []
    for raw in messages:
        view = message_view(raw)
        if view.extensions:
            raise ValueError("当前会话含不可移植的协议扩展，请新建会话。")
        if pending:
            if view.role != "tool" or view.reply_to != pending.pop(0):
                raise ValueError("工具结果未完整配对。")
            results.append(
                {"type": "tool_result", "tool_use_id": view.reply_to, "content": view.text}
            )
            if not pending:
                output.append({"role": "user", "content": results})
                results = []
            continue
        if view.role == "tool":
            raise ValueError("工具结果缺少对应请求。")
        if view.role == "system":
            if output:
                raise ValueError("Messages首版不支持历史中途的system消息。")
            system.append(view.text or "")
            continue
        blocks: list[dict[str, Any]] = []
        if view.text:
            blocks.append({"type": "text", "text": view.text})
        for call in view.calls:
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须为JSON对象。")
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": arguments}
            )
            pending.append(call.id)
        output.append({"role": view.role, "content": blocks})
    if pending:
        raise ValueError("工具结果尚未收齐，不能发送新请求。")
    body: dict[str, Any] = {"messages": output}
    if system:
        body["system"] = "\n\n".join(system)
    return body


def decode_message(data: dict[str, Any]) -> dict[str, Any]:
    """将完整文本/工具响应转回原消息格式；未知块及截断回答明确拒绝。"""
    if data.get("role") != "assistant" or not isinstance(data.get("content"), list):
        raise ValueError("无效助手响应。")
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in data["content"]:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text.append(block["text"])
        elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
            calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block["input"], ensure_ascii=False),
                    },
                }
            )
        else:
            raise ValueError("本版不支持该内容块，未丢弃后继续。")
    if data.get("stop_reason") != ("tool_use" if calls else "end_turn"):
        raise ValueError("回答未完整结束。")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    validate_assistant(message)
    return message
