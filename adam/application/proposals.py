"""将真实子轮读取转换为只读修改候选；不申请批准、不写入目标文件。"""

import secrets
from hashlib import sha256
from io import StringIO

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import StorageError, ToolError
from adam.contracts.proposals import (
    EditProposal,
    ProposalOwner,
    ReadObservation,
    decode_submission,
    observe_read,
)
from adam.session.storage import SessionStore
from adam.tool_calls.edit_plan import plan_edit
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.text_file import TextSnapshot, read_snapshot


class ProposalSubmission:
    """宿主固定派发身份和权限；模型只能提供读取 ID、旧/新文本及理由。"""

    def __init__(
        self,
        store: SessionStore,
        owner: ProposalOwner,
        policy: PathPolicy,
        limits: ToolLimits,
        cancel: CancellationToken | None = None,
    ) -> None:
        self.store, self.owner, self.policy, self.limits = store, owner, policy, limits
        self.cancel = cancel or CancellationToken()
        self._source: dict[str, str] = {}

    def bind_source(self, source: dict[str, str]) -> None:
        """接收执行器固定的当前工具来源；模型参数不能设置这些身份。"""
        self._source = dict(source)

    def submit(self, read_call_id: str, old_text: str, new_text: str, reason: str) -> EditProposal:
        """保存一个文件的一处替换候选；版本、权限、可见正文和唯一性任一失败都不保存。

        返回值只是已落盘候选。当前文件检查后仍可能被外部修改，主任务应用前必须再次校验。
        日志 IO/契约故障作为 StorageError 停止，不降级为可以继续尝试的模型参数错误。
        """
        self.cancel.check()
        session_id = self.owner.child_session_id
        try:
            edit = decode_submission(
                {
                    "read_call_id": read_call_id,
                    "old_text": old_text,
                    "new_text": new_text,
                    "reason": reason,
                }
            )
            turn_id = self._source["turn_id"]
            recorded = self.store.tool_record(session_id, turn_id, read_call_id)
            observed = observe_read(recorded, turn_id)
            if edit.old_text not in observed.text:
                raise ValueError("old text was not present in the returned read window")
            snapshot = self._current(observed)
            plan = plan_edit(
                snapshot,
                edit.old_text,
                edit.new_text,
                observed.evidence.version,
                self.limits.max_text_file_bytes,
            )
            proposal = EditProposal(
                secrets.token_hex(8),
                self.owner,
                turn_id,
                self._source["request_id"],
                self._source["tool_call_id"],
                observed.evidence,
                edit,
                sha256(plan.candidate).hexdigest(),
            )
            self.store.validate_proposal(session_id, proposal)
        except (ValueError, KeyError) as error:
            raise ToolError(f"提案未保存：{error}") from None
        self.cancel.check()
        try:
            self.store.append_proposal(session_id, proposal)
        except ValueError as error:
            raise StorageError("提案日志校验失败；未修改文件，已停止本轮。") from error
        return proposal

    def _current(self, observed: ReadObservation) -> TextSnapshot:
        """在当前读取权限下核对原路径、字节版本及每一可见行，拒绝过期或行内截断证据。"""
        evidence = observed.evidence
        try:
            path = self.policy.resolve(evidence.path)
            if str(path) != evidence.path:
                raise ToolError("读取目标的真实路径已变化，请重新读取后提交提案。")
            snapshot = read_snapshot(path, self.limits.max_text_file_bytes, self.cancel)
        except OSError as error:
            raise ToolError("提案目标无法读取；请检查路径和权限。") from error
        if snapshot.version != evidence.version:
            raise ToolError("文件已变化，请重新读取后提交提案。")
        lines = [line.rstrip("\r\n") for line in StringIO(snapshot.text, newline=None)]
        if any(number > len(lines) or lines[number - 1] != text for number, text in observed.lines):
            raise ToolError("读取行已被截断或与原文件不符；请分段完整读取要修改的内容。")
        return snapshot
