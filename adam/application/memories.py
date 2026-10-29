"""记忆应用操作；协调当前项目、来源及显式管理，不依赖终端或模型。"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from adam.config.settings import MemoryConfig
from adam.context.builder import estimate_json_tokens
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryCommand, MemoryRecord, MemorySelection, MemorySource
from adam.memory.project import project_identity
from adam.memory.retrieval import rank_records
from adam.memory.store import MemoryStore


class MemoryService:
    """用工作目录确定项目，延迟访问磁盘；用户管理不创建聊天会话。"""

    def __init__(
        self, store: MemoryStore, workspace: Path, limits: MemoryConfig | None = None
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.limits = limits or MemoryConfig()

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
            return [self.store.add(command.content, project, source, expires_at=command.expires_at)]
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
                )
            ]
        return [
            self.store.delete(
                command.memory_id, project, source, expected_revision=command.revision
            )
        ]

    def select(self, query: str, available_tokens: int) -> MemorySelection:
        """按相关性选择完整记录；可选记忆读取失败时返回带警告的空快照。

        每轮重读有效条目，不挤占系统、本轮及保护历史。
        仅降级 MemoryStoreError；参数错误、会话日志和请求记账错误仍向上传播。
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
        ranked = rank_records(records, query, datetime.now(UTC))
        selection = MemorySelection(query=query, status="no_match" if not ranked else "budget")
        budget = min(available_tokens, self.limits.max_input_tokens)
        for record in ranked:
            candidate = replace(selection, records=(*selection.records, record), status="selected")
            size = sum(estimate_json_tokens(message) for message in candidate.messages())
            if size <= budget:
                selection = candidate
                if len(selection.records) >= self.limits.max_entries:
                    break
        return selection

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
