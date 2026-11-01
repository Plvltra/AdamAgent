"""独立知识管理入口；仅显式 review 调用模型，发布必须核对候选与审查后确认。"""

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

from adam.config.loader import adam_home, load_config
from adam.config.settings import AppConfig
from adam.contracts.errors import AgentError
from adam.documents.index import DocumentIndex
from adam.documents.knowledge_access import KnowledgeAccess
from adam.documents.knowledge_lifecycle import KnowledgeLifecycle
from adam.documents.knowledge_proposals import KnowledgeProposals
from adam.documents.knowledge_review import KnowledgeModelReviewer
from adam.documents.models import DocumentError
from adam.documents.snapshots import load_snapshot
from adam.llm.client import LLMClient
from adam.tool_calls.policy import PathPolicy


def main(argv: list[str] | None = None) -> int:
    """使用主配置的资料/权限/持久目录；查询不请求模型，显式 review 独立留证和计费。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=adam_home() / "config.toml")
    parser.add_argument("--llm-config", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="查看当前资料范围的候选及其已保存状态")
    for action in ("show", "history", "publish", "reject", "withdraw", "review"):
        command = commands.add_parser(action)
        command.add_argument("candidate_id")
        if action in {"reject", "withdraw"}:
            command.add_argument("reason")
        if action == "review":
            command.add_argument("--max-known-tokens", type=int, default=50000)
    propose = commands.add_parser("propose", help="从授权的 JSON 文件提出候选，不审查或发布")
    propose.add_argument("path", type=Path)
    current = commands.add_parser("current", help="查看当前有效的已发布知识，保留完整引文")
    current.add_argument("query", nargs="?", default="")
    current.add_argument("--offset", type=int, default=0)
    current.add_argument("--limit", type=int, default=5)
    current.add_argument("--version", default="")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, args.llm_config)
        access, policy = _service(config, args.workspace.resolve())
        result = _dispatch(args, config, access, policy)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("知识操作已中断；保留已落盘记录，不自动重发请求。")
        return 130
    except (AgentError, OSError, ValueError) as error:
        print(
            str(error)
            if isinstance(error, AgentError)
            else "知识操作失败，请检查路径和 JSON 格式。"
        )
        return 1


def _service(config: AppConfig, workspace: Path) -> tuple[KnowledgeAccess, PathPolicy]:
    """与聊天组装使用相同范围和保留目录，索引缓存与知识历史分别存储。"""
    documents = config.documents_dir or config.sessions_dir / "_documents"
    knowledge = config.knowledge_dir or config.sessions_dir / "_knowledge"
    protected = (
        documents,
        knowledge,
        config.memories_dir or config.sessions_dir / "_memories",
        config.sessions_dir / "_checkpoints",
        config.sessions_dir / "_artifacts",
        config.sessions_dir / "_mcp_media",
    )
    if not config.documents.roots:
        raise DocumentError("请先在 documents.roots 配置资料目录；不会自动扫描其他目录。")
    policy = PathPolicy(workspace, config.permissions, protected_dirs=protected)
    index = DocumentIndex(documents, workspace, config.documents, policy.resolve)
    return (
        KnowledgeAccess(
            KnowledgeLifecycle(KnowledgeProposals(knowledge, index)),
            max_chars=config.tool_limits.max_result_chars,
        ),
        policy,
    )


def _dispatch(
    args: argparse.Namespace, config: AppConfig, access: KnowledgeAccess, policy: PathPolicy
) -> object:
    """只在明确的 review 分支创建 HTTP 连接；其他分支仅访问本地资料和知识状态。"""
    lifecycle = access.lifecycle
    if args.command == "list":
        return lifecycle.candidates()
    if args.command == "show":
        return lifecycle.inspect(args.candidate_id)
    if args.command == "history":
        return lifecycle.history(args.candidate_id)
    if args.command == "current":
        return access.query(args.query, offset=args.offset, limit=args.limit, version=args.version)
    if args.command == "propose":
        path = policy.resolve(str(args.path))
        with path.open("rb") as source:
            raw = source.read(131073)
        if len(raw) > 131072:
            raise DocumentError("知识候选输入文件超过 128 KiB；没有保存或请求模型。")
        return access.propose(json.loads(raw))
    if args.command == "reject":
        return lifecycle.reject(args.candidate_id, args.reason)
    if args.command == "withdraw":
        return lifecycle.withdraw(args.candidate_id, args.reason)
    if args.command == "publish":
        return lifecycle.publish(args.candidate_id, _confirm)
    return _review(args, config, lifecycle)


def _review(
    args: argparse.Namespace, config: AppConfig, lifecycle: KnowledgeLifecycle
) -> dict[str, Any]:
    """独立审查有自己的单次预算和请求记录；不暗中消耗已有聊天轮次的额度。"""
    print("本次审查会把候选及所引用原文发送给配置的模型，单独计费；不会自动发布。")
    directory = lifecycle.proposals.root / "reviews"
    with httpx.Client(trust_env=False) as http:
        reviewer = KnowledgeModelReviewer(
            LLMClient(config.llm, http),
            config.llm.model,
            directory,
            max_known_tokens=args.max_known_tokens,
        )
        view = lifecycle.review(args.candidate_id, reviewer)
    record = load_snapshot(directory / (view["state"]["review_id"] + ".json"))
    if record is None:
        raise DocumentError("审查请求记录不可读；请核对已保存状态，不自动重发。")
    return {
        "result": view,
        "usage": record["usage"],
        "request_record": str(directory / (record["review_id"] + ".json")),
    }


def _confirm(view: dict[str, Any]) -> bool:
    """展示具体候选、支持/反证及审查意见；只有用户输入 yes 才表示采用。"""
    print(json.dumps(view, ensure_ascii=False, indent=2))
    try:
        return input("确认采用上述知识？输入 yes 发布，其他输入拒绝：").strip().casefold() == "yes"
    except EOFError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
