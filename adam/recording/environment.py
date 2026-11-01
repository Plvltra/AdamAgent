"""将记忆、规则、协作状态等外部资料接口接到同一录制/重放顺序，不替换上下文构造。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.contracts.delegation import Delegation
from adam.contracts.execution import ExecutionMode
from adam.contracts.memory import MemorySelection, decode_record, validate_selection
from adam.contracts.ports import ReviewParticipant
from adam.contracts.rules import ProjectRules, RuleDocument, RuleSelection, validate_rules
from adam.contracts.runtime import CountState, RuntimeState, StateReader, decode_runtime_state
from adam.contracts.skills import SkillListing, SkillSelection, validate_skill_selection

Exchange = Callable[[str, str, dict[str, Any], Callable[[], Any]], Any]


def state_reader(exchange: Exchange, live: StateReader | None = None) -> StateReader:
    """冻结宿主状态来源；离线重放仍比较实际循环次数，不读取当前工作目录或线程。"""

    def read(sid: str, tid: str, requests: CountState, tools: CountState) -> RuntimeState:
        value = exchange(
            "context",
            "state",
            {
                "session_id": sid,
                "turn_id": tid,
                "requests": requests.as_record(),
                "tools": tools.as_record(),
            },
            lambda: live(sid, tid, requests, tools).as_record() if live else None,
        )
        state = decode_runtime_state(value)
        if state.requests != requests or state.tools != tools:
            raise ValueError("观察状态与真实循环次数不符。")
        return state

    return read


def decode_memory(value: dict[str, Any]) -> MemorySelection:
    """从记录重建不可变记忆资料；不访问当前记忆库。"""
    validate_selection(value)
    return MemorySelection(
        tuple(decode_record(item) for item in value["records"]),
        value["query"],
        value["status"],
        value["warning"],
        strategy=value["strategy"],
        embedding=value.get("retrieval", {}).get("embedding"),
        fallback_reason=value.get("retrieval", {}).get("fallback_reason"),
        scores=tuple(tuple(pair) for pair in value.get("retrieval", {}).get("scores", [])),
    )


def decode_skill(
    value: dict[str, Any] | None, catalog: list[dict[str, str]]
) -> SkillSelection | None:
    """重建显式选择正文，仍校验来源、目录版本及内容哈希。"""
    if value is None:
        return None
    validate_skill_selection(value, catalog)
    listing = dict(value["catalog"])
    listing["identifier"] = listing.pop("id")
    return SkillSelection(
        SkillListing(**listing), value["version"], value["content"], tuple(value["resources"])
    )


def memory_provider(
    exchange: Exchange, live: Callable[[str, int], MemorySelection] | None = None
) -> Callable[[str, int], MemorySelection]:
    """适配每轮一次的记忆选择；真实预算容量作为输入比较，资料仅来自所选来源。"""

    def select(query: str, capacity: int) -> MemorySelection:
        value = exchange(
            "memory",
            "select",
            {"query": query, "capacity": capacity},
            lambda: live(query, capacity).to_dict() if live else None,
        )
        return decode_memory(value)

    return select


def mode_provider(
    exchange: Exchange, live: Callable[[], ExecutionMode] | None = None
) -> Callable[[], ExecutionMode]:
    """保存本轮实际模式；重放仍通过原 ContextBuilder 加入模式约束。"""
    return lambda: ExecutionMode(
        exchange("context", "mode", {}, lambda: live().value if live else None)
    )


class ObservedRules:
    """录制真实规则跟踪器的资料；离线时不重新扫描规则文件。"""

    def __init__(self, exchange: Exchange, live: ProjectRules | None = None) -> None:
        self.exchange, self.live = exchange, live

    def begin(self) -> None:
        """保留规则轮次开始的调用与失败。"""
        self.exchange("rules", "begin", {}, lambda: self.live.begin() if self.live else None)

    def end(self) -> None:
        """保留结束清理；不把外部读取失败吞成无规则。"""
        self.exchange("rules", "end", {}, lambda: self.live.end() if self.live else None)

    def snapshot(self, *, preview: bool = False) -> RuleSelection | None:
        """获取已记录的范围、正文和版本，生成消息仍由 RuleSelection 完成。"""

        def read() -> dict[str, Any] | None:
            result = self.live.snapshot(preview=preview) if self.live else None
            return result.to_record() if result is not None else None

        value = self.exchange("rules", "snapshot", {"preview": preview}, read)
        if value is None:
            return None
        validate_rules(value)
        return RuleSelection(
            value["workspace"], tuple(RuleDocument(**item) for item in value["documents"])
        )

    def acknowledge(self, selection: RuleSelection | None) -> None:
        """比较模型真正收到的规则快照，再让在线跟踪器确认。"""
        self.exchange(
            "rules",
            "acknowledge",
            {"selection": selection.to_record() if selection else None},
            lambda: self.live.acknowledge(selection) if self.live else None,
        )

    def check(self, targets: tuple[Path, ...]) -> None:
        """仅供宿主保留原规则检查；重放不会启动真实工具预检。"""
        self.exchange(
            "rules",
            "check",
            {"targets": [str(path) for path in targets]},
            lambda: self.live.check(targets) if self.live else None,
        )


class ObservedDelegation:
    """替代子任务调度边界的状态输入；主 Conversation 的收尾判断照常执行。"""

    def __init__(self, exchange: Exchange, live: Delegation | None = None) -> None:
        self.exchange, self.live = exchange, live

    def begin(self, session_id: str, turn_id: str, model: str) -> None:
        """绑定来源，离线模式只比较调用而不启动线程。"""
        self.exchange(
            "delegation",
            "begin",
            {"session_id": session_id, "turn_id": turn_id, "model": model},
            lambda: self.live.begin(session_id, turn_id, model) if self.live else None,
        )

    def set_memory(self, selection: MemorySelection) -> None:
        """比较主任务传给子任务的记忆快照。"""
        self.exchange(
            "delegation",
            "set_memory",
            {"selection": selection.to_dict()},
            lambda: self.live.set_memory(selection) if self.live else None,
        )

    def instructions(self, session_id: str | None) -> str:
        """返回当时的子任务状态提示，不读取今天的子会话索引。"""
        return self.exchange(
            "delegation",
            "instructions",
            {"session_id": session_id},
            lambda: self.live.instructions(session_id) if self.live else None,
        )

    def accept_final(self) -> bool:
        """重放子结果是否已收集的观察，主循环仍决定继续请求或提交回答。"""
        return self.exchange(
            "delegation",
            "accept_final",
            {},
            lambda: self.live.accept_final() if self.live else None,
        )

    def end(self) -> None:
        """在线清理本组任务，离线仅消费原清理结果。"""
        self.exchange("delegation", "end", {}, lambda: self.live.end() if self.live else None)

    def close(self) -> None:
        """宿主资源关闭不属于被选轮次；离线没有待关闭的真实子任务。"""
        if self.live:
            self.live.close()


class ObservedReview:
    """保留审查边界提供的状态与提示；不在线下重新读取文件或派发审查者。"""

    def __init__(self, exchange: Exchange, live: ReviewParticipant | None = None) -> None:
        self.exchange, self.live = exchange, live
        self.session_id = ""

    def begin(self, session_id: str, turn_id: str) -> None:
        """保留当前轮次身份，供主循环判断审查提示是否属于本会话。"""
        self.exchange(
            "review",
            "begin",
            {"session_id": session_id, "turn_id": turn_id},
            lambda: self.live.begin(session_id, turn_id) if self.live else None,
        )
        self.session_id = session_id

    def pending(self) -> bool:
        """读取当时未收集状态，仍由主循环决定是否接受最终回答。"""
        return self.exchange(
            "review", "pending", {}, lambda: self.live.pending() if self.live else None
        )

    def instructions(self) -> str:
        """重放原审查状态提示，保留实际子任务结果来源。"""
        return self.exchange(
            "review", "instructions", {}, lambda: self.live.instructions() if self.live else None
        )

    def finish(self) -> None:
        """保留原文件复查与收尾的结果；重放不再次触碰被审文件。"""
        self.exchange("review", "finish", {}, lambda: self.live.finish() if self.live else None)
