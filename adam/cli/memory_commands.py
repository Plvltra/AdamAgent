"""显式记忆命令的参数解析与展示；正文保持原文，业务经应用接口执行。"""

import argparse
import json
import re
import shlex
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Never

from adam.application.facade import AgentApplication
from adam.contracts.errors import MemoryInputError
from adam.contracts.memory import MemoryCommand

USAGE = (
    "/memory candidates | candidate ID | accept ID [-- 改写正文] | discard ID\n"
    "/memory add [--global] [--expires 带时区ISO时间] -- 正文\n"
    "/memory list [--global] [--history ID]\n"
    "/memory update ID --revision N [--global] [--expires 时间] -- 新正文\n"
    "/memory delete ID --revision N [--global]\n"
    "默认项目范围；update 未给 --expires 会取消原过期时间。"
)


class MemoryParser(argparse.ArgumentParser):
    """解析失败交给统一业务错误展示，不退出终端也不写 stderr。"""

    def error(self, message: str) -> Never:
        raise MemoryInputError("记忆命令参数无效。用法：\n" + USAGE)


def handle_experience_command(
    text: str, application: AgentApplication, write: Callable[[str], None]
) -> None:
    """候选操作经应用边界执行；展示明确区分建议、人工接受和验证事实。"""
    options, separator, content = text.partition(" -- ")
    parts = options.split()
    action = parts[0]
    if (
        (action == "candidates" and len(parts) != 1)
        or (action != "candidates" and len(parts) != 2)
        or (separator and (action != "accept" or not content.strip()))
    ):
        raise MemoryInputError("候选命令参数无效。用法：\n" + USAGE)
    if action in {"accept", "discard"}:
        record = application.decide_experience(parts[1], action, content)
        if record is None:
            write("经验> 已丢弃候选。")
        else:
            state = "已删除的原记录，未恢复" if record.deleted else "已保存或先前已接受"
            write(f"经验> {state}：{record.memory_id} v{record.revision}；尚未验证。")
        return
    candidates = application.experience_candidates()
    if action == "candidate":
        candidates = [c for c in candidates if c["reflection_id"] == parts[1]]
        if not candidates:
            raise MemoryInputError("当前会话没有该经验候选。")
    if not candidates:
        write("经验> 当前会话没有候选。")
    for candidate in candidates:
        write(f"经验> {candidate['reflection_id']} [{candidate['candidate_status']}] 尚未验证")
        write("  " + json.dumps(candidate["result"], ensure_ascii=False))
        if action == "candidate":
            write("  真实来源资料：" + json.dumps(candidate["source"], ensure_ascii=False))
            write("  保存记忆：" + json.dumps(candidate["memory"], ensure_ascii=False))


def parse_memory_command(text: str) -> MemoryCommand:
    """用独立的 -- 分隔正文，避免 shell 解析破坏 Windows 路径和正文引号。"""
    delimiter = re.search(r"(?:^|\s)--(?:\s|$)", text)
    if delimiter:
        options, content = text[: delimiter.start()], text[delimiter.end() :].strip()
    else:
        options, content = text, ""
    parser = MemoryParser(add_help=False, allow_abbrev=False)
    parser.add_argument("action", choices=("add", "list", "update", "delete"))
    parser.add_argument("memory_id", nargs="?", default="")
    parser.add_argument("--global", dest="global_scope", action="store_true")
    parser.add_argument("--expires", dest="expires_at")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--history", default="")
    try:
        arguments = parser.parse_args(shlex.split(options))
    except ValueError:
        raise MemoryInputError("记忆命令选项引号未闭合。") from None
    if arguments.history and arguments.memory_id:
        raise MemoryInputError("--history 不接受额外 ID。")
    return MemoryCommand(
        arguments.action,
        content,
        arguments.history or arguments.memory_id,
        arguments.revision,
        arguments.global_scope,
        arguments.expires_at,
        bool(arguments.history),
    )


def handle_memory_command(
    text: str, application: AgentApplication, write: Callable[[str], None]
) -> None:
    """展示查询结果或已保存版本；任何失败都不会提前打印成功。"""
    if not text.strip() or text.strip() == "help":
        write(USAGE)
        return
    if text.split(maxsplit=1)[0] in {"candidates", "candidate", "accept", "discard"}:
        handle_experience_command(text, application, write)
        return
    command = parse_memory_command(text)
    records = application.manage_memory(command)
    if not records:
        write("记忆> 当前范围暂无记录。")
        return
    if command.action != "list":
        write("记忆> 已保存。")
    now = datetime.now(UTC)
    for record in records:
        state = "已删除" if record.deleted else ("有效" if record.active(now) else "已过期")
        scope = record.project or "全局"
        write(f"记忆> {record.memory_id} v{record.revision} [{scope}] {state}：{record.content}")
        if record.experience:
            write("  经验：尚未验证；来源 " + json.dumps(record.experience, ensure_ascii=False))
        write(
            f"  来源：用户 {record.source.operation}；会话 {record.source.session_id or '无'}；"
            f"创建 {record.created_at}；更新 {record.updated_at}；过期 {record.expires_at or '无'}"
        )
