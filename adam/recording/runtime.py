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
from adam.application.subagents.unit_budget import UnitBudget
from adam.config.consumption import DEFAULT_CONSUMPTION, ConsumptionConfig, parse_consumption
from adam.config.settings import AgentConfig, ContextConfig, PlanningConfig, ReflectionConfig
from adam.contracts.consumption import ConsumptionSnapshot, Quantities
from adam.contracts.delegation import RequestAccount
from adam.contracts.diagnostic_context import DIAGNOSTIC_CONTEXT_VERSION
from adam.contracts.errors import AgentError, ConfigError
from adam.contracts.history import validate_compaction_version, validate_context_layout
from adam.contracts.media import MediaRef
from adam.contracts.model_input import media_records, message_media
from adam.contracts.models import ModelOutput
from adam.contracts.ports import ToolRunner
from adam.contracts.runtime import RUNTIME_STATE_VERSION
from adam.contracts.skills import SkillSelection, validate_catalog
from adam.recording.capture import RecordingTools, TurnRecorder
from adam.recording.environment import (
    Exchange,
    ObservedDelegation,
    ObservedReview,
    ObservedRules,
    decode_skill,
    diagnostic_reader,
    memory_provider,
    mode_provider,
    state_reader,
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
            **(
                {"consumption": conversation.consumption.as_record()}
                if conversation.consumption.enabled
                else {}
            ),
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
                {"diagnostic_context_version": DIAGNOSTIC_CONTEXT_VERSION}
                if conversation.diagnostic_reader
                else {}
            ),
            **(
                {"compaction_version": conversation.compaction_version}
                if conversation.compaction_version is not None
                else {}
            ),
            **(
                {"runtime_state_version": RUNTIME_STATE_VERSION}
                if conversation.state_reader and conversation.context_builder.layout_version
                else {}
            ),
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
        if conversation.diagnostic_reader:
            conversation.diagnostic_reader = diagnostic_reader(
                self.exchange, conversation.diagnostic_reader
            )
        previous_media_observer = conversation.observe_media_input

        def observe_media(references: tuple[MediaRef, ...]) -> None:
            if self.recorder.active:
                self.recorder.capture_media(references)
            self.exchange(
                "context", "input_media", {"media": media_records(references)}, lambda: None
            )
            if previous_media_observer:
                previous_media_observer(references)

        conversation.observe_media_input = observe_media
        if conversation.state_reader:
            conversation.state_reader = state_reader(self.exchange, conversation.state_reader)
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
                        {
                            "session_id": session_id,
                            "estimate": state["estimate"],
                            **(
                                {"units": state["requested_units"]}
                                if "requested_units" in state
                                else {}
                            ),
                        },
                        lambda: {
                            key: value
                            for key, value in state.items()
                            if key not in {"estimate", "requested_units"}
                        },
                    )

                result.on_snapshot = snapshot
                return result

            conversation.accounting = account
            conversation.requests.accounting = account


class ReplayAccount:
    """使用当时其他成员的已观察消费与预留，当前主会话消费仍读取真实重放日志。"""

    def __init__(
        self,
        tape: ReplayTape,
        store: ReplayStore,
        maximum: int,
        consumption: ConsumptionConfig = DEFAULT_CONSUMPTION,
    ) -> None:
        self.tape, self.store, self.maximum = tape, store, maximum
        self.consumption = consumption
        self.owner = ""

    def _budget(
        self, operation: str, estimate: int | None = None, units: Quantities | None = None
    ) -> SharedTokenBudget:
        value = self.tape.exchange(
            "accounting",
            operation,
            {
                "session_id": self.store.session_id,
                "estimate": estimate,
                **({"units": units.as_record()} if units is not None else {}),
            },
            lambda: dict[str, Any](),
        )
        if (
            not isinstance(value, dict)
            or value.keys()
            != {"owner", "members", "reserved"} | ({"units"} if self.consumption.enabled else set())
            or not isinstance(value["owner"], str)
            or not isinstance(value["members"], list)
        ):
            raise RecordingError("共享额度快照格式无效。")
        unit_budget = None
        if self.consumption.enabled:
            try:
                unit_budget = UnitBudget.from_record(self.consumption.limits(), value["units"])
            except (ValueError, TypeError, KeyError) as error:
                raise RecordingError("独立单位额度快照格式无效。") from error
            for member in value["units"]["members"]:
                if member["owner"] == value["owner"]:
                    actual_units = self.store.consumption_usage(self.store.session_id)
                    recorded = ConsumptionSnapshot.from_record(
                        {key: item for key, item in member.items() if key != "owner"}
                    )
                    self.tape.compare(
                        recorded.as_record(), actual_units.as_record(), "$.accounting.parent_units"
                    )
                    unit_budget.update(member["owner"], actual_units)
        self.owner = value["owner"]
        budget = SharedTokenBudget(self.maximum, reserved=value["reserved"], units=unit_budget)
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
            member_units = None
            if unit_budget is not None:
                found = next(
                    (
                        item
                        for item in value["units"]["members"]
                        if item["owner"] == member["owner"]
                    ),
                    None,
                )
                if found is None:
                    raise RecordingError("独立单位与 token 账本成员不一致。")
                member_units = ConsumptionSnapshot.from_record(
                    {key: item for key, item in found.items() if key != "owner"}
                )
            budget.update(member["owner"], tokens, unknown, units=member_units)
        if value["owner"] not in seen:
            raise RecordingError("共享额度快照缺少主会话。")
        if (
            unit_budget is not None
            and {member["owner"] for member in value["units"]["members"]} != seen
        ):
            raise RecordingError("独立单位与 token 账本成员不一致。")
        return budget

    def check(self) -> None:
        """当前限额由真实 SharedTokenBudget 判断，未采用录制的成功布尔值。"""
        self._budget("check").check()

    def usage(self) -> tuple[int, bool]:
        """按原成员快照与当前主日志计算整组消费。"""
        return self._budget("usage").usage()

    @contextmanager
    def request(self, estimate: int, *, units: Quantities | None = None) -> Iterator[None]:
        """当前 token/独立单位估算重新参与预留，失败和退出沿真实预算作用域处理。"""
        budget = self._budget("reserve", estimate, units)
        with budget.reserve(estimate, units=units):
            yield
            if budget.units is not None:
                self._budget("settled").check_units()


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
        or boot.keys()
        - {
            "context_layout",
            "runtime_state_version",
            "compaction_version",
            "diagnostic_context_version",
            "consumption",
        }
        != flags | {"skills"}
        or any(type(boot[key]) is not bool for key in flags)
        or not isinstance(arguments, dict)
        or arguments.keys() != {"text", "selected_skill"}
        or arguments["text"] != recording.seed["user_text"]
    ):
        raise RecordingError("录制运行资料的字段或输入无效。")
    try:
        if "consumption" in boot:
            config = parse_consumption(boot["consumption"])
            if (
                not config.enabled
                or not boot["accounting"]
                or config.as_record() != boot["consumption"]
            ):
                raise ValueError("unit-enabled replay requires accounting")
        validate_context_layout(boot.get("context_layout"))
        if "diagnostic_context_version" in boot and (
            boot["diagnostic_context_version"] != DIAGNOSTIC_CONTEXT_VERSION
            or not boot.get("context_layout")
        ):
            raise ValueError("诊断观察版本或布局无效。")
        validate_compaction_version(boot.get("compaction_version"))
        if "compaction_version" in boot and boot["compaction_version"] is None:
            raise ValueError("missing compaction version")
        if "runtime_state_version" in boot and (
            boot["runtime_state_version"] != RUNTIME_STATE_VERSION or not boot.get("context_layout")
        ):
            raise ValueError("运行状态版本或布局无效。")
        validate_catalog(boot["skills"])
        decode_skill(arguments["selected_skill"], boot["skills"])
    except (ValueError, TypeError, KeyError, ConfigError) as error:
        raise RecordingError("录制的上下文布局、摘要版本、状态或技能资料无效。") from error
    return boot, arguments


class ReplayMediaInput:
    """离线入口只使用加载时校验过的证据；不猜测当前供应商能力或读取原文件。"""

    def __init__(self, recording: Recording, store: ReplayStore) -> None:
        self.recording, self.store = recording, store

    def validate(self, references: tuple[MediaRef, ...], model: str) -> None:
        """核对完整冻结身份与当前诊断会话；未验证副本不可作为媒体输入。"""
        media_records(references)
        if (
            not self.recording.media_verified
            or model != self.recording.seed["model"]
            or any(
                ref not in self.recording.media
                or ref.source.session_id != self.store.active_session_id
                for ref in references
            )
        ):
            raise AgentError("离线输入媒体不在已验证的录制中。")


def recorded_input_media(recording: Recording) -> tuple[MediaRef, ...]:
    """取得原宿主显式提交的附件；工具输出和历史媒体不冒充本轮输入。"""
    for event in recording.events[1:]:
        if event["type"] == "observation" and event["data"]["channel"] == "context":
            value = event["data"]["value"]
            if value.get("operation") == "input_media":
                return message_media({"role": "user", **value["input"]})
    return ()


def build_replay(
    tape: ReplayTape,
    store: ReplayStore,
    *,
    agent: AgentConfig | None = None,
    context: ContextConfig | None = None,
) -> tuple[Conversation, SkillSelection | None]:
    """仅构造生产内部流程及离线端口；带媒体时须已验证旁路副本。

    不导入工具工厂、HTTP、MCP 或真实子调度器。
    """
    if tape.recording.media and not tape.recording.media_verified:
        raise RecordingError("录制仅解析了事件，媒体副本尚未验证，不能回放。")
    boot, arguments = runtime_start(tape.recording)
    seed = tape.recording.seed
    controls = seed["controls"]
    limits = agent or AgentConfig(**controls["agent"])
    context_limits = context or ContextConfig(**controls["context"])
    consumption = parse_consumption(boot.get("consumption", {}))
    exchange: Exchange = tape.exchange

    def account(session_id: str) -> RequestAccount:
        tape.exchange("accounting", "select", {"session_id": session_id}, lambda: None)
        return ReplayAccount(tape, store, limits.max_session_tokens, consumption)

    conversation = Conversation(
        store,
        ReplayModel(tape, store),
        seed["session_events"][0]["payload"]["system_prompt"],
        ReplayTools(tape, store),
        limits,
        model=seed["model"],
        media_input=ReplayMediaInput(tape.recording, store),
        context_config=context_limits,
        consumption=consumption,
        # 缺少布局标记的旧录制继续使用旧消息顺序和估算，不伪造相同请求。
        context_layout=boot.get("context_layout"),
        compaction_version=boot.get("compaction_version"),
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
        state_reader=state_reader(exchange) if "runtime_state_version" in boot else None,
        diagnostic_reader=diagnostic_reader(exchange)
        if "diagnostic_context_version" in boot
        else None,
    )

    def observe(text: str, selection: SkillSelection | None) -> None:
        tape.exchange(
            "context",
            "runtime",
            {"text": text, "selected_skill": selection.to_record() if selection else None},
            lambda: boot,
        )

    conversation.observe_input = observe
    conversation.observe_media_input = lambda references: tape.exchange(
        "context", "input_media", {"media": media_records(references)}, lambda: None
    )
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
                media=recorded_input_media(recording),
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
