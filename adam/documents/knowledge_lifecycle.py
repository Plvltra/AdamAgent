"""独立审查、用户发布与撤回；状态另存，冻结候选及其证据保持原样。"""

import copy
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import islice
from typing import Any

from filelock import Timeout

from adam.documents.evidence_audit import audit_summary
from adam.documents.knowledge_proposals import MAX_CANDIDATES, KnowledgeProposals, validate_proposal
from adam.documents.knowledge_states import (
    KnowledgeReviewer,
    fold_events,
    initial_state,
    validate_review,
    validate_text,
)
from adam.documents.models import DocumentError
from adam.documents.snapshots import load_snapshot, save_snapshot


class KnowledgeLifecycle:
    """宿主控制审查者和确认回调；模型意见不能直接触发已发布状态。"""

    def __init__(self, proposals: KnowledgeProposals) -> None:
        self.proposals = proposals
        self.path = proposals.root / "states.json"

    def inspect(self, candidate_id: str) -> dict[str, Any]:
        """返回冻结候选与持久化状态；source_status=not_checked，不代表它仍可用于当前回答。"""
        with self._locked():
            candidate = self.proposals.read(candidate_id)
            ledger = self._load()
            return self._view(candidate, self._state(ledger, candidate))

    def candidates(self) -> list[dict[str, Any]]:
        """列出当前范围的候选摘要与已保存状态；不自动审查或声称来源仍当前。"""
        with self._locked():
            paths = list(islice(self.proposals.directory.glob("*.json"), MAX_CANDIDATES + 1))
            if len(paths) > MAX_CANDIDATES:
                raise DocumentError("知识候选超过读取上限，请先归档已处理记录。")
            ledger = self._load()
            result = []
            for path in sorted(paths):
                candidate = self.proposals.read(path.stem)
                state = self._state(ledger, candidate)
                result.append(
                    {
                        "candidate_id": path.stem,
                        "statement": candidate["proposal"]["statement"],
                        "created_at": candidate["created_at"],
                        "status": state["status"],
                        "revision": state["revision"],
                        "source_status": "not_checked",
                    }
                )
            return result

    def review(self, candidate_id: str, reviewer: KnowledgeReviewer) -> dict[str, Any]:
        """对独立审查前后分别核对来源和状态；审查运行期间不占文件锁，也不自动重试。

        reviewer 只能由宿主注入，返回值只是意见。期间若发生拒绝、其他审查或源修改，
        旧结果不能覆盖新决定；失败不会留下可用于发布的通过状态。
        """
        reviewer_name = reviewer.identity
        validate_text(reviewer_name, maximum=200)
        with self._locked():
            candidate = self.proposals.read(candidate_id)
            previous = self._state(self._load(), candidate)
            if previous["status"] not in {"pending", "reviewed", "changes_requested"}:
                raise DocumentError("该知识候选已发布、拒绝或撤回，不能重新审查原候选。")
            self._current(candidate)
        review_id = secrets.token_hex(8)
        opinion = copy.deepcopy(reviewer.review(copy.deepcopy(candidate), review_id))
        proposal = candidate["proposal"]
        expected = {c["chunk_id"] for c in proposal["support"] + proposal["counterevidence"]}
        validate_review(opinion, expected)
        with self._locked():
            ledger = self._load()
            self._unchanged(ledger, candidate, previous)
            self._current(candidate)
            state = self._append(
                ledger,
                previous,
                "reviewed" if opinion["verdict"] == "accept" else "changes_requested",
                "独立审查已完成；意见仍须用户核对。",
                "reviewer",
                proposal["source_generation"],
                review=opinion,
                reviewer=reviewer_name,
                review_id=review_id,
            )
            return self._view(candidate, state)

    def publish(
        self, candidate_id: str, confirm: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any]:
        """展示当前候选与审查后申请用户确认；确认期间变化时拒绝，不沿用旧批准。

        confirm 是宿主界面回调，不接受模型传布尔值。重复发布当前有效版本不重复确认或
        写事件；拒绝写入独立终止状态，发布不改变候选原文，也不证明知识语义正确。
        """
        if not callable(confirm):
            raise DocumentError("知识发布需要宿主提供明确用户确认。")
        with self._locked():
            candidate = self.proposals.read(candidate_id)
            previous = self._state(self._load(), candidate)
            self._current(candidate)
            if previous["status"] == "published":
                return self._view(candidate, previous)
            if previous["status"] != "reviewed":
                raise DocumentError("知识候选尚未通过独立审查或已终止，未请求发布确认。")
        approved = confirm(self._view(candidate, previous))
        if type(approved) is not bool:
            raise DocumentError("知识发布确认必须返回明确的布尔结果。")
        with self._locked():
            ledger = self._load()
            self._unchanged(ledger, candidate, previous)
            if approved:
                self._current(candidate)
            state = self._append(
                ledger,
                previous,
                "published" if approved else "rejected",
                "用户明确确认采用。" if approved else "用户拒绝本次知识候选。",
                "user",
                candidate["proposal"]["source_generation"] if approved else None,
            )
            return self._view(candidate, state)

    def reject(self, candidate_id: str, reason: str) -> dict[str, Any]:
        """用户拒绝尚未发布候选；保留来源和审查历史，不允许原候选后来被自动采用。"""
        return self._user_stop(
            candidate_id, reason, "rejected", {"pending", "reviewed", "changes_requested"}
        )

    def withdraw(self, candidate_id: str, reason: str) -> dict[str, Any]:
        """用户撤回已发布或待复核知识；源文件不可读时也能撤回，不删除原历史。"""
        return self._user_stop(candidate_id, reason, "withdrawn", {"published", "needs_review"})

    def current(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """只返回已发布、来源仍当前且在有效期内的知识；源改变先追加待复核事件。

        对整个配置语料使用同一版本，因此无关文档变动也会保守失效；不自动重新审查或
        重发请求。权限/保存故障直接停止，不能把无权校验解释成原知识仍可用。
        """
        now = now or datetime.now(UTC)
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise DocumentError("知识查询时间必须包含时区。")
        with self._locked():
            ledger = self._load()
            published = [
                s
                for s in fold_events(ledger, self.proposals.scope).values()
                if s["status"] == "published"
            ]
            if not published:
                return []
            corpus = self.proposals.index.refresh()
            result = []
            for state in published:
                self.proposals.index.check_cancel()
                candidate = self.proposals.read(state["candidate_id"])
                self._state(ledger, candidate)
                proposal = candidate["proposal"]
                if proposal["source_generation"] != corpus.generation:
                    self._append(
                        ledger,
                        state,
                        "needs_review",
                        "来源版本已变化，停止当前复用；需要新候选。",
                        "source_change",
                        corpus.generation,
                    )
                    continue
                start, end = proposal["valid_from"], proposal["valid_until"]
                if (start is None or datetime.fromisoformat(start) <= now) and (
                    end is None or now < datetime.fromisoformat(end)
                ):
                    view = self._view(candidate, state)
                    view["source_status"] = "current_snapshot"
                    result.append(view)
            return result

    def history(self, candidate_id: str) -> list[dict[str, Any]]:
        """返回该候选的决定历史，不将旧发布事件当作当前有效状态。"""
        with self._locked():
            candidate = self.proposals.read(candidate_id)
            ledger = self._load()
            self._state(ledger, candidate)
            return copy.deepcopy([e for e in ledger["events"] if e["candidate_id"] == candidate_id])

    def _user_stop(
        self, identity: str, reason: str, target: str, allowed: set[str]
    ) -> dict[str, Any]:
        validate_text(reason)
        with self._locked():
            candidate = self.proposals.read(identity)
            ledger = self._load()
            previous = self._state(ledger, candidate)
            if previous["status"] == target:
                return self._view(candidate, previous)
            if previous["status"] not in allowed:
                raise DocumentError("该知识候选状态不允许此操作。")
            state = self._append(ledger, previous, target, reason, "user", None)
            return self._view(candidate, state)

    def _load(self) -> dict[str, Any]:
        """状态不是可丢弃缓存；损坏时保留原文件，禁止自动重建成空的批准历史。"""
        try:
            ledger = load_snapshot(self.path)
            if ledger is None:
                ledger = {
                    "format": "knowledge_states_v1",
                    "scope": self.proposals.scope,
                    "events": [],
                }
            fold_events(ledger, self.proposals.scope)
            return ledger
        except DocumentError as error:
            raise DocumentError("知识决定历史损坏；保留原文件，未恢复、发布或清空历史。") from error

    def _state(self, ledger: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        identity, version = candidate["candidate_id"], candidate["content_hash"]
        state = fold_events(ledger, self.proposals.scope).get(
            identity, initial_state(identity, version)
        )
        if state["candidate_hash"] != version:
            raise DocumentError("知识候选与决定历史的内容版本不一致。")
        return state

    def _current(self, candidate: dict[str, Any]) -> None:
        if self.proposals.index.scope != self.proposals.scope:
            raise DocumentError("知识资料范围已变化，请重新建立服务。")
        if (
            audit_summary(self.proposals.index, validate_proposal(candidate["proposal"]))["status"]
            != "provenance_valid"
        ):
            raise DocumentError("知识来源已变化或证据失效，请生成新候选；未发布。")

    def _unchanged(
        self, ledger: dict[str, Any], candidate: dict[str, Any], previous: dict[str, Any]
    ) -> None:
        current = self.proposals.read(candidate["candidate_id"])
        if current != candidate or self._state(ledger, current) != previous:
            raise DocumentError("知识候选或决定已被其他操作更新；本次旧审查/确认未采用。")

    def _append(
        self,
        ledger: dict[str, Any],
        previous: dict[str, Any],
        status: str,
        reason: str,
        decision_source: str,
        generation: str | None,
        *,
        review: dict[str, Any] | None = None,
        reviewer: str | None = None,
        review_id: str | None = None,
    ) -> dict[str, Any]:
        """先校验整条状态链，再原子保存；保存失败不报告新状态已生效。"""
        event = {
            **previous,
            **(
                {"review": review, "reviewer": reviewer, "review_id": review_id}
                if review is not None
                else {}
            ),
            "revision": previous["revision"] + 1,
            "status": status,
            "reason": reason,
            "at": datetime.now(UTC).isoformat(),
            "decision_source": decision_source,
            "observed_generation": generation,
        }
        candidate = {**ledger, "events": [*ledger["events"], event]}
        fold_events(candidate, self.proposals.scope)
        self.proposals.index.check_cancel()
        save_snapshot(self.path, candidate)
        ledger["events"] = candidate["events"]
        return event

    def _view(self, candidate: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(
            {
                "candidate": candidate,
                "state": state,
                "source_status": "not_checked",
                "semantic_status": "not_verified",
            }
        )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """与候选保存共用同一把锁；网络及界面回调由调用方放在锁外。"""
        self.proposals.index.check_cancel()
        try:
            self.proposals.root.mkdir(parents=True, exist_ok=True)
            with self.proposals.lock:
                yield
        except Timeout as error:
            raise DocumentError("知识状态正被其他进程更新，请稍后重试。") from error
        except OSError as error:
            raise DocumentError("知识状态读写失败；未报告新决定成功，原历史保留。") from error
