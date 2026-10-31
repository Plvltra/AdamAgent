"""不可变修改提案与真实读取证据；只校验数据，不访问文件或授予执行权限。"""

import json
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from adam.contracts.models import ExecutionRecorder

MAX_PROPOSALS_PER_TURN = 8
MAX_PATCH_BYTES = 32768
MAX_READ_BYTES = 65536


@dataclass(frozen=True)
class ProposalOwner:
    """宿主固定的主/子派发身份；父关联由主任务收集时再与实际委派索引核对。"""

    parent_session_id: str
    parent_turn_id: str
    child_session_id: str
    run_id: str

    def __post_init__(self) -> None:
        for value, width in (
            (self.parent_session_id, 8),
            (self.parent_turn_id, 32),
            (self.child_session_id, 8),
            (self.run_id, 16),
        ):
            _hex(value, width)


@dataclass(frozen=True)
class ReadEvidence:
    """同一子轮实际 read_file 的身份、原字节版本和返回行范围；不是模型自报路径。"""

    request_id: str
    tool_call_id: str
    path: str
    version: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        _hex(self.request_id, 32)
        _text(self.tool_call_id, 1024)
        _text(self.path, 32768)
        _hex(self.version, 64)
        if not Path(self.path).is_absolute():
            raise ValueError("read evidence requires an absolute path")
        if (
            type(self.start_line) is not int
            or type(self.end_line) is not int
            or not 1 <= self.start_line <= self.end_line
        ):
            raise ValueError("invalid read evidence lines")


@dataclass(frozen=True)
class ProposalEdit:
    """模型允许提出的四个字段；旧/新文本规范为 LF，路径与版本由读取记录取得。"""

    read_call_id: str
    old_text: str
    new_text: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.read_call_id, 1024)
        _text(self.old_text, MAX_PATCH_BYTES)
        _text(self.new_text, MAX_PATCH_BYTES, allow_empty=True)
        _text(self.reason, 2000)
        if not self.reason.strip() or len(self.reason) > 500:
            raise ValueError("proposal reason must contain 1 to 500 characters")
        if "\r" in self.old_text or "\r" in self.new_text or self.old_text == self.new_text:
            raise ValueError("proposal requires changed canonical LF text")


@dataclass(frozen=True)
class EditProposal:
    """已保存的候选；存在不表示批准或应用，target_version 来自纯编辑规划。"""

    proposal_id: str
    owner: ProposalOwner
    turn_id: str
    request_id: str
    tool_call_id: str
    read: ReadEvidence
    edit: ProposalEdit
    target_version: str

    def __post_init__(self) -> None:
        _hex(self.proposal_id, 16)
        _hex(self.turn_id, 32)
        _hex(self.request_id, 32)
        _text(self.tool_call_id, 1024)
        _hex(self.target_version, 64)
        if (
            not isinstance(self.owner, ProposalOwner)
            or not isinstance(self.read, ReadEvidence)
            or not isinstance(self.edit, ProposalEdit)
        ):
            raise ValueError("invalid proposal components")
        if (
            self.edit.read_call_id != self.read.tool_call_id
            or self.target_version == self.read.version
        ):
            raise ValueError("invalid proposal read or target version")

    def to_dict(self) -> dict[str, Any]:
        """生成独立 JSON 载荷；不带可被模型伪造的批准或应用状态。"""
        return asdict(self)


@dataclass(frozen=True)
class ReadObservation:
    """真实工具结果的完整可见行；text 不补造末行换行或被截断部分。"""

    evidence: ReadEvidence
    lines: tuple[tuple[int, str], ...]

    @property
    def text(self) -> str:
        return "\n".join(text for _, text in self.lines)


class ProposalSource(Protocol):
    """宿主核对所属主会话、实际派发和完整成功子轮后提供候选。"""

    def candidates(self, agent_id: str, run_id: str) -> tuple[EditProposal, ...]: ...


class ProposalSubmitter(Protocol):
    """辅助子任务只能保存有读取依据的候选。"""

    def submit(
        self, read_call_id: str, old_text: str, new_text: str, reason: str
    ) -> EditProposal: ...


class ProposalReviewer(Protocol):
    """主任务审核工具需要的窄接口；实现负责来源、分页和审批执行。"""

    def collect(
        self, agent_id: str, run_id: str, index: int = 0, offset: int = 0
    ) -> dict[str, Any]: ...

    def validate_apply(self, value: object) -> None: ...

    def apply(
        self,
        agent_id: str,
        run_id: str,
        proposal_id: str,
        path: str,
        record: ExecutionRecorder | None,
    ) -> dict[str, Any]: ...


def decode_submission(value: object) -> ProposalEdit:
    """只接收补丁与读取调用 ID；CRLF 参数归一化，额外的路径/版本/身份字段拒绝。"""
    data = _object(value, ProposalEdit)
    for name in ("old_text", "new_text"):
        if not isinstance(data[name], str):
            raise ValueError("proposal text must be a string")
        data[name] = data[name].replace("\r\n", "\n")
    return ProposalEdit(**data)


def decode_proposal(value: object) -> EditProposal:
    """写入和回放采用相同严格结构；未知字段与非规范文本不能绕过校验。"""
    data = _object(value, EditProposal)
    data["owner"] = ProposalOwner(**_object(data["owner"], ProposalOwner))
    data["read"] = ReadEvidence(**_object(data["read"], ReadEvidence))
    data["edit"] = ProposalEdit(**_object(data["edit"], ProposalEdit))
    return EditProposal(**data)


def observe_read(tool: dict[str, Any], turn_id: str) -> ReadObservation:
    """从同轮已完成读取取得证据；失败、无正文预览和非连续行不作为读取依据。"""
    if (
        tool.get("turn_id") != turn_id
        or tool.get("name") != "read_file"
        or tool.get("finished") is not True
    ):
        raise ValueError("proposal requires a finished read_file in this child turn")
    try:
        args, output = json.loads(tool["arguments"]), json.loads(tool["output"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("invalid recorded read") from error
    if not isinstance(args, dict) or not isinstance(output, dict) or output.get("ok") is not True:
        raise ValueError("proposal read was not successful")
    lines = output.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 1000:
        raise ValueError("proposal requires visible read lines, not a preview")
    start = args.get("start_line", 1)
    if type(start) is not int or start < 1:
        raise ValueError("invalid recorded read start")
    visible = []
    for position, line in enumerate(lines, start):
        if (
            not isinstance(line, dict)
            or set(line) != {"line", "text"}
            or type(line["line"]) is not int
            or line["line"] != position
            or not isinstance(line["text"], str)
            or "\n" in line["text"]
            or "\r" in line["text"]
        ):
            raise ValueError("read lines are not a contiguous text window")
        visible.append((position, line["text"]))
    _text("\n".join(text for _, text in visible), MAX_READ_BYTES, allow_empty=True)
    try:
        evidence = ReadEvidence(
            tool["request_id"],
            tool["tool_call_id"],
            output["path"],
            output["version"],
            start,
            start + len(visible) - 1,
        )
    except (KeyError, TypeError) as error:
        raise ValueError("read result lacks source path or version") from error
    return ReadObservation(evidence, tuple(visible))


def validate_proposal_read(
    proposal: EditProposal, tools: dict[tuple[str, str], dict[str, Any]]
) -> None:
    """回放只核对已落盘的来源及可见旧文本；当前版本和替换唯一性由宿主读文件验证。"""
    tool = tools.get((proposal.turn_id, proposal.read.tool_call_id), {})
    observed = observe_read(tool, proposal.turn_id)
    if observed.evidence != proposal.read or proposal.edit.old_text not in observed.text:
        raise ValueError("proposal does not match its actual read evidence")


def _object(value: object, model: type) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(model)}:
        raise ValueError("invalid proposal fields")
    return dict(value)


def _hex(value: str, width: int) -> None:
    if not isinstance(value, str) or re.fullmatch(f"[0-9a-f]{{{width}}}", value) is None:
        raise ValueError("invalid proposal identifier or version")


def _text(value: str, max_bytes: int, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or "\0" in value or (not value and not allow_empty):
        raise ValueError("invalid proposal text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise ValueError("invalid proposal text encoding") from error
    if size > max_bytes:
        raise ValueError("proposal text exceeds byte limit")
