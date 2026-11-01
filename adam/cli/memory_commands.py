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
from adam.contracts.memory import MemoryCommand, MemoryReference, MemorySubject, timestamp

USAGE = (
    "/memory tidy [ID] [--global]：生成或查看离线整理候选，不修改记忆\n"
    "/memory tidy accept 整理ID 候选ID [--global] [-- 整理正文]\n"
    "/memory tidy undo 概览ID --revision N [--global]\n"
    "/memory candidates | candidate ID | accept ID [-- 改写正文] | discard ID\n"
    "/memory add [--global] [--expires 带时区ISO时间] -- 正文\n"
    "/memory list [--global] [--history ID]\n"
    "/memory update ID --revision N [--global] [--expires 时间] -- 新正文\n"
    "/memory delete ID --revision N [--global]\n"
    "add/update 可加 --subject 主体ID --subject-name 显示名、--valid-from 带时区时间；\n"
    "add 可加 --supersedes 旧ID --supersedes-revision N，原子结束旧事实有效期。\n"
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
    parser.add_argument("--subject")
    parser.add_argument("--subject-name")
    parser.add_argument("--valid-from")
    parser.add_argument("--supersedes")
    parser.add_argument("--supersedes-revision", type=int)
    try:
        arguments = parser.parse_args(shlex.split(options))
    except ValueError:
        raise MemoryInputError("记忆命令选项引号未闭合。") from None
    if arguments.history and arguments.memory_id:
        raise MemoryInputError("--history 不接受额外 ID。")
    if (arguments.subject is None) != (arguments.subject_name is None):
        raise MemoryInputError("主体 ID 与显示名须一起提供。")
    if (arguments.supersedes is None) != (arguments.supersedes_revision is None):
        raise MemoryInputError("被替代的旧 ID 与版本须一起提供。")
    return MemoryCommand(
        arguments.action,
        content,
        arguments.history or arguments.memory_id,
        arguments.revision,
        arguments.global_scope,
        arguments.expires_at,
        bool(arguments.history),
        subject=MemorySubject(arguments.subject, arguments.subject_name)
        if arguments.subject is not None
        else None,
        valid_from=arguments.valid_from,
        supersedes=MemoryReference(arguments.supersedes, arguments.supersedes_revision)
        if arguments.supersedes is not None
        else None,
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
    if text.split(maxsplit=1)[0] == "tidy":
        handle_organization(text, application, write)
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
        if (
            not record.deleted
            and record.valid_from is not None
            and timestamp(record.valid_from) > now
        ):
            state = "尚未生效"
        scope = record.project or "全局"
        write(f"记忆> {record.memory_id} v{record.revision} [{scope}] {state}：{record.content}")
        if record.experience:
            write("  经验：尚未验证；来源 " + json.dumps(record.experience, ensure_ascii=False))
        if record.subject:
            write(f"  主体：@{record.subject.key}（{record.subject.name}）；同名不合并身份。")
        if record.valid_from:
            write(f"  生效：{record.valid_from}")
        if record.supersedes:
            write(
                f"  替代：{record.supersedes.memory_id} v{record.supersedes.revision}；旧版本仍可追溯。"
            )
        if record.derivation:
            origin = record.derivation
            references = ", ".join(f"{s.memory_id} v{s.revision}" for s in origin.sources)
            write(
                f"  整理概览：{origin.organization_id}/{origin.candidate_id}；尚未验证；来源 {references}"
            )
        write(
            f"  来源：用户 {record.source.operation}；会话 {record.source.session_id or '无'}；"
            f"创建 {record.created_at}；更新 {record.updated_at}；过期 {record.expires_at or '无'}"
        )


def handle_organization(
    text: str, application: AgentApplication, write: Callable[[str], None]
) -> None:
    """显示可逐项核对的原文和版本；保存候选成功不代表已采纳整理。"""
    parts = text.split(maxsplit=2)
    if len(parts) > 1 and parts[1] in {"accept", "undo"}:
        handle_organization_decision(text, application, write)
        return
    parser = MemoryParser(add_help=False, allow_abbrev=False)
    parser.add_argument("action", choices=("tidy",))
    parser.add_argument("identity", nargs="?")
    parser.add_argument("--global", dest="global_scope", action="store_true")
    try:
        arguments = parser.parse_args(shlex.split(text))
    except ValueError:
        raise MemoryInputError("整理命令选项引号未闭合。") from None
    organization = application.organize_memory(
        arguments.identity, global_scope=arguments.global_scope
    )
    write(
        f"整理> {organization.organization_id} [{organization.project or '全局'}] 仅为候选，原记忆未修改。"
    )
    labels = {"merge": "合并候选", "keep": "保留", "conflict": "可能冲突", "expired": "过期"}
    for candidate in organization.candidates:
        write(f"  {candidate.candidate_id} {labels[candidate.kind]}：{candidate.reason}")
        for record in candidate.records:
            subject = f" @{record.subject.key}（{record.subject.name}）" if record.subject else ""
            write(f"    {record.memory_id} v{record.revision}{subject}：{record.content}")


def handle_organization_decision(
    text: str, application: AgentApplication, write: Callable[[str], None]
) -> None:
    """明确采纳一个候选或撤销概览；正文沿用独立 -- 分隔，不解释为 shell 参数。"""
    delimiter = re.search(r"(?:^|\s)--(?:\s|$)", text)
    options = text[: delimiter.start()] if delimiter else text
    content = text[delimiter.end() :].strip() if delimiter else None
    parser = MemoryParser(add_help=False, allow_abbrev=False)
    parser.add_argument("command", choices=("tidy",))
    parser.add_argument("action", choices=("accept", "undo"))
    parser.add_argument("identity")
    parser.add_argument("candidate", nargs="?")
    parser.add_argument("--revision", type=int)
    parser.add_argument("--global", dest="global_scope", action="store_true")
    try:
        args = parser.parse_args(shlex.split(options))
    except ValueError:
        raise MemoryInputError("整理命令选项引号未闭合。") from None
    if args.action == "accept":
        if args.candidate is None or args.revision is not None:
            raise MemoryInputError("采纳须提供整理 ID 和候选 ID，不接受 --revision。")
        record = application.accept_memory_organization(
            args.identity, args.candidate, global_scope=args.global_scope, content=content
        )
        state = "已有概览已删除，未恢复" if record.deleted else "已保存或先前已接受"
        write(f"整理> {state}：{record.memory_id} v{record.revision}；尚未验证。")
    else:
        if args.candidate is not None or args.revision is None or content is not None:
            raise MemoryInputError("撤销须提供概览 ID 与 --revision，不接受候选 ID 或正文。")
        record = application.undo_memory_organization(
            args.identity, args.revision, global_scope=args.global_scope
        )
        write(f"整理> 已撤销概览 {record.memory_id}，来源已恢复原有效期；历史保留。")
