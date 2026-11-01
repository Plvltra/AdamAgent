"""记忆应用操作；协调当前项目、来源及显式管理，不依赖终端或模型。"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from adam.config.settings import MemoryConfig
from adam.context.builder import estimate_json_tokens
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemoryRecord, MemorySelection, MemorySource
from adam.contracts.memory_organization import MemoryOrganization
from adam.memory.organization import load_organization, propose, save_organization
from adam.memory.project import project_identity
from adam.memory.retrieval import rank_records
from adam.memory.semantic import MemoryRanking, SemanticMemoryRanker
from adam.memory.store import MemoryStore
from adam.memory.subjects import select_subjects


class MemoryService:
    """用工作目录确定项目，延迟访问磁盘；用户管理不创建聊天会话。"""

    def __init__(
        self,
        store: MemoryStore,
        workspace: Path,
        limits: MemoryConfig | None = None,
        *,
        semantic: SemanticMemoryRanker | None = None,
    ) -> None:
        """注入项目范围、选择预算和可选本地排序器；管理操作不依赖 embedding。"""
        self.store = store
        self.workspace = workspace
        self.limits = limits or MemoryConfig()
        self.semantic = semantic

    def manage(self, command: MemoryCommand, session_id: str | None = None) -> list[MemoryRecord]:
        """执行已解析的用户命令，返回实际持久化版本或查询快照。

        默认只修改当前项目；全局修改必须显式选择。更新和删除要求版本号，
        因而另一个终端在用户查看之后做出的编辑不会被静默覆盖。
        """
        self._validate(command)
        project = None if command.global_scope else project_identity(self.workspace)
        source = MemorySource(command.action, session_id)
        if command.action == "list":
            if command.history:
                return list(self.store.history(command.memory_id, project))
            return self.store.list_records(project, include_global=not command.global_scope)
        if command.action == "add":
            return [
                self.store.add(
                    command.content,
                    project,
                    source,
                    expires_at=command.expires_at,
                    subject=command.subject,
                    valid_from=command.valid_from,
                    supersedes=command.supersedes,
                )
            ]
        if command.revision is None:
            raise MemoryInputError("更新或删除须提供 --revision，先用 /memory list 查看版本。")
        if command.action == "update":
            return [
                self.store.update(
                    command.memory_id,
                    project,
                    command.content,
                    source,
                    expected_revision=command.revision,
                    expires_at=command.expires_at,
                    subject=command.subject,
                    valid_from=command.valid_from,
                )
            ]
        return [
            self.store.delete(
                command.memory_id, project, source, expected_revision=command.revision
            )
        ]

    def organize(
        self, identity: str | None = None, *, global_scope: bool = False
    ) -> MemoryOrganization:
        """手动生成或读取当前范围的离线候选；不改写记忆、不调用模型、不混合全局和项目。"""
        project = None if global_scope else project_identity(self.workspace)
        if identity is not None:
            return load_organization(self.store.root, identity, project)
        records = self.store.list_records(project)
        organization = propose(records, project, datetime.now(UTC))
        save_organization(self.store.root, organization)
        return organization

    def accept_organization(
        self,
        identity: str,
        candidate_id: str,
        *,
        global_scope: bool = False,
        content: str | None = None,
        session_id: str | None = None,
    ) -> MemoryRecord:
        """确认具体候选，保存概览及完整来源；冲突正文须由用户明确提供。"""
        plan = self.organize(identity, global_scope=global_scope)
        candidate = next((c for c in plan.candidates if c.candidate_id == candidate_id), None)
        if candidate is None:
            raise MemoryInputError("该整理记录中没有此候选。")
        return self.store.consolidate(
            candidate, identity, plan.project, MemorySource("add", session_id), content=content
        )

    def undo_organization(
        self,
        memory_id: str,
        revision: int,
        *,
        global_scope: bool = False,
        session_id: str | None = None,
    ) -> MemoryRecord:
        """撤销尚未修改的整理概览，原子恢复原记录；不隐式扩大项目范围。"""
        project = None if global_scope else project_identity(self.workspace)
        return self.store.undo_consolidation(
            memory_id, project, MemorySource("delete", session_id), expected_revision=revision
        )

    def select(self, query: str, available_tokens: int) -> MemorySelection:
        """按相关性选择完整记录；可选记忆读取失败时返回带警告的空快照。

        每轮重读有效条目，不挤占系统、本轮及保护历史。
        先按当前范围、事实有效期和显式主体筛选，再做相关性排序；同名不猜身份。
        读取故障返回空资料；语义后端故障或超限明确降级关键词。
        参数错误、会话日志和请求记账错误仍向上传播。
        """
        if not self.limits.enabled:
            return MemorySelection(status="disabled")
        if not query.strip():
            return MemorySelection(status="query_required")
        try:
            project = project_identity(self.workspace)
            records = self.store.list_records(project, include_global=True)
        except MemoryStoreError as error:
            return MemorySelection(
                query=query,
                status="unavailable",
                warning=f"记忆读取失败，本轮未加载记忆；聊天继续。{error.user_message}",
            )
        records, subject_warning = select_subjects(
            [record for record in records if record.active(datetime.now(UTC))], query
        )
        ranking = self._rank(records, query)
        selection = MemorySelection(
            query=query,
            status="no_match" if not ranking.records else "budget",
            strategy=ranking.strategy,
            embedding=ranking.embedding,
            fallback_reason=ranking.fallback_reason,
            warning=subject_warning
            + (
                "语义记忆检索不可用或超限，本轮已降级为关键词检索。"
                if ranking.fallback_reason
                else ""
            ),
        )
        budget = min(available_tokens, self.limits.max_input_tokens)
        scores = dict(ranking.scores)
        for record in ranking.records:
            candidate = replace(
                selection,
                records=(*selection.records, record),
                status="selected",
                scores=(*selection.scores, (record.memory_id, scores[record.memory_id]))
                if ranking.strategy == "semantic_v1"
                else (),
            )
            size = sum(estimate_json_tokens(message) for message in candidate.messages())
            if size <= budget:
                selection = candidate
                if len(selection.records) >= self.limits.max_entries:
                    break
        return selection

    def _rank(self, records: list[MemoryRecord], query: str) -> MemoryRanking:
        """仅把存储已按项目过滤的快照交给排序器，不用全库向量先查后过滤。"""
        now = datetime.now(UTC)
        if self.limits.method == "semantic" and self.semantic is not None:
            return self.semantic.rank(records, query, now)
        fallback = self.limits.method == "semantic"
        return MemoryRanking(
            tuple(rank_records(records, query, now)),
            "keyword_fallback_v1" if fallback else "keyword_v1",
            fallback_reason="backend_missing" if fallback else None,
        )

    @staticmethod
    def _validate(command: MemoryCommand) -> None:
        """校验应用入口，禁止通过非 CLI 调用绕过操作与范围约束。"""
        if command.action not in {"add", "list", "update", "delete"}:
            raise MemoryInputError("未知记忆操作，请使用 add、list、update 或 delete。")
        if type(command.global_scope) is not bool or type(command.history) is not bool:
            raise MemoryInputError("记忆范围与历史选项无效。")
        if command.action in {"add", "update"}:
            if not isinstance(command.content, str) or not command.content.strip():
                raise MemoryInputError("记忆正文不能为空；用 -- 分隔选项与正文。")
        elif command.content or command.expires_at is not None:
            raise MemoryInputError("查询或删除不接受正文和过期时间。")
        needs_id = command.action in {"update", "delete"} or command.history
        if needs_id != bool(command.memory_id):
            raise MemoryInputError("此操作的记忆 ID 缺失或多余。")
        if command.history and command.action != "list":
            raise MemoryInputError("--history 只用于 list。")
        if command.action in {"add", "list"} and command.revision is not None:
            raise MemoryInputError("此操作不接受 --revision。")
        if command.action not in {"add", "update"} and (
            command.subject is not None or command.valid_from is not None
        ):
            raise MemoryInputError("主体和生效时间仅用于 add 或 update。")
        if command.action != "add" and command.supersedes is not None:
            raise MemoryInputError("替代关系仅用于 add，新事实需关联旧 ID 与版本。")
