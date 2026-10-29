"""协调经验候选与项目记忆；两个文件之间以来源幂等处理保存中断。"""

from typing import Any

from adam.application.memories import MemoryService
from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import MemoryRecord, MemorySource
from adam.memory.project import project_identity
from adam.session.storage import SessionStore


class ExperienceService:
    """候选来自已落盘反思；人工接受前不进入跨会话检索。"""

    def __init__(self, sessions: SessionStore, memories: MemoryService) -> None:
        self.sessions, self.memories = sessions, memories

    def candidates(self, session_id: str) -> list[dict[str, Any]]:
        """列出本会话候选及来源；记忆已保存而决定日志缺失时显示待对账状态。"""
        decisions = self.sessions.experience_decisions(session_id)
        project = project_identity(self.memories.workspace)
        result = []
        for record in self.sessions.experience_candidates(session_id):
            origin = self._origin(session_id, record)
            memory = self.memories.store.find_experience(origin, project)
            decision = decisions.get(record["reflection_id"], {})
            state = decision.get("action", "saved_pending_log" if memory else "pending")
            result.append(
                {
                    **record,
                    "candidate_status": state,
                    "verification": "unverified",
                    "memory": memory.to_dict() if memory else None,
                }
            )
        return result

    def decide(
        self, session_id: str, candidate_id: str, action: str, content: str = ""
    ) -> MemoryRecord | None:
        """接受可改写正文，丢弃不可带正文；先保存记忆再记决定，重试不覆盖或复活旧版本。

        会话写锁由应用持有。两个文件无法同时原子提交，记忆库中的稳定来源用于恢复；
        任何保存失败向上传播，不提前报告成功。
        """
        if action not in {"accept", "discard"} or (action == "discard" and content):
            raise MemoryInputError("经验操作只支持 accept 或 discard；discard 不接受正文。")
        candidate = next(
            (c for c in self.candidates(session_id) if c["reflection_id"] == candidate_id), None
        )
        if candidate is None:
            raise MemoryInputError("当前会话没有该经验候选。")
        state = candidate["candidate_status"]
        if state == "discarded":
            if action == "discard":
                return None
            raise MemoryInputError("该候选已丢弃，不能再次接受。")
        if state == "accepted" and candidate["memory"] is None:
            raise MemoryStoreError(
                "候选已接受但当前记忆库缺少来源记录；请检查记忆目录，不自动重建。"
            )
        if candidate["turn_status"] == "in_progress":
            raise MemoryInputError("请在轮次结束后处理候选。")
        if action == "discard":
            if candidate["memory"] is not None or state == "accepted":
                raise MemoryInputError("该经验已保存；请用 /memory delete 管理现有记忆。")
            self.sessions.decide_experience(session_id, candidate_id, "discarded")
            return None
        suggestion = candidate["result"]
        text = (
            content.strip()
            if content
            else (
                suggestion["lesson"]
                + "\n适用："
                + suggestion["applies_when"]
                + "\n限制："
                + (suggestion["limits"] or "仅供参考，需结合当前任务核验")
            )
        )
        record = self.memories.store.add(
            text,
            project_identity(self.memories.workspace),
            MemorySource("add", session_id),
            experience=self._origin(session_id, candidate),
        )
        self.sessions.decide_experience(session_id, candidate_id, "accepted", record.memory_id)
        return record

    @staticmethod
    def _origin(session_id: str, record: dict[str, Any]) -> dict[str, str]:
        """来源随记忆版本保留，接受和后续修改均不把未验证自动变为已验证。"""
        return {
            "session_id": session_id,
            "turn_id": record["turn_id"],
            "feedback_id": record["feedback_id"],
            "reflection_id": record["reflection_id"],
            "verification": "unverified",
        }
