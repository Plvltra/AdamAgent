"""组装单轮录制与离线流程；使用生产 Conversation、计划、反思、摘要及预算，不构造外部执行器。"""

import json
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar

from adam.application.conversation import Conversation
from adam.application.planning import PlanningService
from adam.application.reflection import ReflectionService
from adam.application.subagents.account import SessionAccount
from adam.application.subagents.budget import SharedTokenBudget
from adam.config.settings import AgentConfig, ContextConfig, PlanningConfig, ReflectionConfig
from adam.contracts.delegation import RequestAccount
from adam.contracts.history import validate_context_layout
from adam.contracts.models import ModelOutput
from adam.contracts.ports import ToolRunner
from adam.contracts.skills import SkillSelection, validate_catalog
from adam.recording.capture import RecordingTools, TurnRecorder
from adam.recording.environment import (
    Exchange,
    ObservedDelegation,
    ObservedReview,
    ObservedRules,
    decode_skill,
    memory_provider,
    mode_provider,
)
from adam.recording.format import RecordingError, error_record
from adam.recording.journal import Recording, read_recording
from adam.recording.replay import ReplayIncompleteError, ReplayMismatchError, ReplayTape
from adam.recording.replay_adapters import ReplayModel, ReplayTools
from adam.recording.replay_store import ReplayStore

_Result = TypeVar("_Result")


class CaptureRuntime:
    """给已组装的主 Conversation 安装资料观察者；实际执行和子线程实现保持原样。"""

    def __init__(self, recorder: TurnRecorder) -> None:
        self.recorder = recorder
        self.conversation: Conversation | None = None

    def tools(self, delegate: ToolRunner) -> ToolRunner:
        """装饰原工具批次及目录查询，供 build_application 的宿主入口使用。"""
        return RecordingTools(delegate, self.recorder, observe_definitions=True)

    def exchange(
        self, channel: str, operation: str, arguments: dict[str, Any], action: Callable[[], _Result]
    ) -> _Result:
        """仅录制被选中的轮次；组装、预览及关闭资源继续直接调用原接口。"""
        if not self.recorder.active:
            return action()
        return self.recorder.exchange(channel, operation, arguments, action)

    def bind(self, conversation: Conversation) -> None:
        """记录完整入口资料并装饰外部资料来源；不替换内部控制服务。"""
        if self.conversation is not None:
            raise RecordingError("一个运行录制器只能绑定一个主会话执行器。")
        self.conversation = conversation
        previous_identity = conversation.store.identity_provider
        conversation.store.identity_provider = lambda kind, size: self.exchange(
            "context",
            "identity",
            {"kind": kind, "size": size},
            lambda: previous_identity(kind, size) if previous_identity else secrets.token_hex(size),
        )
        boot = {
            "planning": conversation.planning is not None,
            "reflection": conversation.reflection is not None,
            "memory": conversation.memory_provider is not None,
            "rules": conversation.rules is not None,
            "delegation": conversation.delegation is not None,
            "review": conversation.review is not None,
            "accounting": conversation.accounting is not None,
            "mode": conversation.mode_provider is not None,
            "skills": list(conversation.skills),
            **(
                {"context_layout": conversation.context_builder.layout_version}
                if conversation.context_builder.layout_version
                else {}
            ),
        }
        previous_observer = conversation.observe_input

        def observe(text: str, selection: SkillSelection | None) -> None:
            self.exchange(
                "context",
                "runtime",
                {"text": text, "selected_skill": selection.to_record() if selection else None},
                lambda: deepcopy(boot),
            )
            if previous_observer:
                previous_observer(text, selection)

        conversation.observe_input = observe
        if conversation.memory_provider:
            conversation.memory_provider = memory_provider(
                self.exchange, conversation.memory_provider
            )
        if conversation.mode_provider:
            conversation.mode_provider = mode_provider(self.exchange, conversation.mode_provider)
        if conversation.rules:
            conversation.rules = ObservedRules(self.exchange, conversation.rules)
        if conversation.delegation:
            conversation.delegation = ObservedDelegation(self.exchange, conversation.delegation)
        if conversation.review:
            conversation.review = ObservedReview(self.exchange, conversation.review)
        if conversation.accounting:
            original_factory = conversation.accounting

            def account(session_id: str) -> RequestAccount:
                selected: list[SessionAccount] = []

                def select() -> None:
                    result = original_factory(session_id)
                    if not isinstance(result, SessionAccount):
                        raise RecordingError("当前账本未实现决策快照，不能录制为可重放运行。")
                    selected.append(result)

                self.exchange("accounting", "select", {"session_id": session_id}, select)
                result = selected[0]

                def snapshot(operation: str, state: dict[str, Any]) -> None:
                    self.exchange(
                        "accounting",
                        operation,
                        {"session_id": session_id, "estimate": state["estimate"]},
                        lambda: {key: value for key, value in state.items() if key != "estimate"},
                    )

                result.on_snapshot = snapshot
                return result

            conversation.accounting = account
            conversation.requests.accounting = account


class ReplayAccount:
    """使用当时其他成员的已观察消费与预留，当前主会话消费仍读取真实重放日志。"""

    def __init__(self, tape: ReplayTape, store: ReplayStore, maximum: int) -> None:
        self.tape, self.store, self.maximum = tape, store, maximum

    def _budget(self, operation: str, estimate: int | None = None) -> SharedTokenBudget:
        value = self.tape.exchange(
            "accounting",
            operation,
            {"session_id": self.store.session_id, "estimate": estimate},
            lambda: dict[str, Any](),
        )
        if (
            not isinstance(value, dict)
            or value.keys() != {"owner", "members", "reserved"}
            or not isinstance(value["owner"], str)
            or not isinstance(value["members"], list)
        ):
            raise RecordingError("共享额度快照格式无效。")
        budget = SharedTokenBudget(self.maximum, reserved=value["reserved"])
        seen = set()
        for member in value["members"]:
            if (
                not isinstance(member, dict)
                or member.keys() != {"owner", "tokens", "unknown"}
                or not isinstance(member["owner"], str)
                or member["owner"] in seen
                or type(member["tokens"]) is not int
                or member["tokens"] < 0
                or type(member["unknown"]) is not bool
            ):
                raise RecordingError("共享额度成员格式无效。")
            seen.add(member["owner"])
            tokens, unknown = member["tokens"], member["unknown"]
            if member["owner"] == value["owner"]:
                actual = self.store.usage(self.store.session_id)
                self.tape.compare([tokens, unknown], list(actual), "$.accounting.parent")
                tokens, unknown = actual
            budget.update(member["owner"], tokens, unknown)
        if value["owner"] not in seen:
            raise RecordingError("共享额度快照缺少主会话。")
        return budget

    def check(self) -> None:
        """当前限额由真实 SharedTokenBudget 判断，未采用录制的成功布尔值。"""
        self._budget("check").check()

    def usage(self) -> tuple[int, bool]:
        """按原成员快照与当前主日志计算整组消费。"""
        return self._budget("usage").usage()

    @contextmanager
    def request(self, estimate: int) -> Iterator[None]:
        """当前请求估算重新参与预留，失败和退出沿真实预算作用域处理。"""
        budget = self._budget("reserve", estimate)
        with budget.reserve(estimate):
            yield


def runtime_start(recording: Recording) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验显式运行起点；缺少资料观察的早期录制只能诊断，不能冒充可重放。"""
    if len(recording.events) < 2:
        raise ReplayIncompleteError("录制缺少运行起点。")
    event = recording.events[1]
    data = event["data"]
    if event["type"] != "observation" or data["channel"] != "context":
        raise RecordingError("录制没有运行资料；请使用完整运行录制入口重新录制。")
    value = data["value"]
    if (
        value.keys() != {"operation", "input", "value", "error"}
        or value["operation"] != "runtime"
        or value["error"] is not None
    ):
        raise RecordingError("录制运行起点无效。")
    boot, arguments = value["value"], value["input"]
    flags = {
        "planning",
        "reflection",
        "memory",
        "rules",
        "delegation",
        "review",
        "accounting",
        "mode",
    }
    if (
        not isinstance(boot, dict)
        or boot.keys() - {"context_layout"} != flags | {"skills"}
        or any(type(boot[key]) is not bool for key in flags)
        or not isinstance(arguments, dict)
        or arguments.keys() != {"text", "selected_skill"}
        or arguments["text"] != recording.seed["user_text"]
    ):
        raise RecordingError("录制运行资料的字段或输入无效。")
    try:
        validate_context_layout(boot.get("context_layout"))
        validate_catalog(boot["skills"])
        decode_skill(arguments["selected_skill"], boot["skills"])
    except (ValueError, TypeError, KeyError) as error:
        raise RecordingError("录制的上下文布局、技能目录或选择无效。") from error
    return boot, arguments


def build_replay(
    tape: ReplayTape,
    store: ReplayStore,
    *,
    agent: AgentConfig | None = None,
    context: ContextConfig | None = None,
) -> tuple[Conversation, SkillSelection | None]:
    """仅构造生产内部流程及离线端口；不导入工具工厂、HTTP、MCP 或真实子调度器。"""
    boot, arguments = runtime_start(tape.recording)
    seed = tape.recording.seed
    controls = seed["controls"]
    limits = agent or AgentConfig(**controls["agent"])
    context_limits = context or ContextConfig(**controls["context"])
    exchange: Exchange = tape.exchange

    def account(session_id: str) -> RequestAccount:
        tape.exchange("accounting", "select", {"session_id": session_id}, lambda: None)
        return ReplayAccount(tape, store, limits.max_session_tokens)

    conversation = Conversation(
        store,
        ReplayModel(tape, store),
        seed["session_events"][0]["payload"]["system_prompt"],
        ReplayTools(tape, store),
        limits,
        model=seed["model"],
        context_config=context_limits,
        # 缺少布局标记的旧录制继续使用旧消息顺序和估算，不伪造相同请求。
        context_layout=boot.get("context_layout"),
        planning=PlanningService(store, PlanningConfig(**controls["planning"]))
        if boot["planning"]
        else None,
        reflection=ReflectionService(
            store, ReflectionConfig(**controls["reflection"]), context_limits
        )
        if boot["reflection"]
        else None,
        memory_provider=memory_provider(exchange) if boot["memory"] else None,
        mode_provider=mode_provider(exchange) if boot["mode"] else None,
        rules=ObservedRules(exchange) if boot["rules"] else None,
        delegation=ObservedDelegation(exchange) if boot["delegation"] else None,
        review=ObservedReview(exchange) if boot["review"] else None,
        accounting=account if boot["accounting"] else None,
        skills=tuple(boot["skills"]),
    )

    def observe(text: str, selection: SkillSelection | None) -> None:
        tape.exchange(
            "context",
            "runtime",
            {"text": text, "selected_skill": selection.to_record() if selection else None},
            lambda: boot,
        )

    conversation.observe_input = observe
    conversation.resume(seed["session_id"])
    return conversation, decode_skill(arguments["selected_skill"], boot["skills"])


def replay_turn(
    source: Path,
    destination: Path,
    *,
    agent: AgentConfig | None = None,
    context: ContextConfig | None = None,
) -> dict[str, Any]:
    """把录制在全新目录中重放，返回一致/差异/缺失报告；原文件不写入，模型和工具不执行。"""
    recording = read_recording(source)
    runtime_start(recording)
    tape = ReplayTape(recording)
    output: list[ModelOutput] = []
    report: dict[str, Any] = {}
    with ReplayStore(destination, tape) as store:
        conversation, selection = build_replay(tape, store, agent=agent, context=context)
        reply, failure = None, None
        # 是否提供展示回调取自实际请求的记录；流式片段由离线适配器逐条交付。
        has_output = any(
            item["type"] == "call_started"
            and item["data"]["name"] == "model"
            and item["data"]["input"]["on_output"]
            for item in recording.events
        )
        try:
            reply = conversation.send(
                recording.seed["user_text"],
                on_output=output.append if has_output else None,
                selected_skill=selection,
            )
        except BaseException as error:
            failure = error
        try:
            if isinstance(failure, ReplayIncompleteError):
                raise failure
            tape.finish(reply, failure)
            store.at_boundary(recording.events[-1]["seq"])
        except ReplayMismatchError as error:
            report = {"status": "different", "difference": asdict(error.difference)}
        except ReplayIncompleteError as error:
            report = {"status": "incomplete", "reason": str(error)}
        else:
            report = {
                "status": "matched",
                "reply": reply,
                "error": recording.events[-1]["data"]["error"],
            }
        # 清理接口也可能观察到差异；诊断同时保留促使流程提前结束的业务原因。
        primary = failure
        seen: set[int] = set()
        while isinstance(primary, ReplayMismatchError) and primary.__context__ is not None:
            if id(primary) in seen:
                break
            seen.add(id(primary))
            primary = primary.__context__
        report.update(
            kind="offline_replay",
            actual_error=error_record(primary) if primary is not None else None,
            consumed_events=tape.position,
            session_events=store.offset,
            output=[{"kind": item.kind, "text": item.text} for item in output],
        )
    with (destination / "report.json").open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.write("\n")
    return report
