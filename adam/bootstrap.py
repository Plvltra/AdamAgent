"""真实执行应用的组装入口；加载配置，创建依赖并在退出或启动失败时释放资源。"""

import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from adam.application.background import BackgroundService
from adam.application.conversation import Conversation
from adam.application.diagnostics import DiagnosticContextReader
from adam.application.event_queue import EventQueue
from adam.application.facade import AgentApplication
from adam.application.memories import MemoryService
from adam.application.modes import ModeController
from adam.application.planning import PlanningService
from adam.application.proposal_review import ProposalReview
from adam.application.proposals import ProposalSubmission
from adam.application.reflection import ReflectionService
from adam.application.review import ReviewService
from adam.application.running_inputs import RunningInputs, recover_inputs
from adam.application.runtime_state import RuntimeStateReader
from adam.application.subagents.account import SessionAccount
from adam.application.subagents.budget import SharedTokenBudget
from adam.application.subagents.manager import SubagentManager
from adam.application.terminals import TerminalService
from adam.application.triggers import LocalTriggers
from adam.application.undo import UndoService
from adam.config.loader import load_config
from adam.config.settings import AppConfig, ArtifactConfig, DocumentConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.delegation import ChildAgent, ChildRun
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.contracts.memory import MemorySelection
from adam.contracts.models import CommandRequest, FileChangePlan
from adam.contracts.planning import PlanReport
from adam.contracts.ports import ModelClient, ToolRunner
from adam.contracts.proposals import ProposalOwner
from adam.contracts.terminals import TerminalRequest
from adam.contracts.verification import TaskVerifier
from adam.documents.embedding import FastEmbedBackend
from adam.hooks.runner import HookRunner
from adam.isolation.backend import WslBubblewrap
from adam.isolation.commands import IsolatedCommands
from adam.isolation.rpc import IsolatedOrchestrator
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.mcp.media_store import MCPMediaStore
from adam.memory.semantic import SemanticMemoryRanker
from adam.memory.store import MemoryStore
from adam.rules.catalog import RuleCatalog
from adam.rules.tracker import RuleTracker
from adam.session.artifacts import ArtifactStore
from adam.session.storage import SessionStore
from adam.tool_calls.artifact_tools import ToolArtifacts
from adam.tool_calls.background_jobs import BackgroundJobs
from adam.tool_calls.background_tools import BackgroundTools
from adam.tool_calls.command_tools import CommandTools
from adam.tool_calls.delegation_tools import SPECS as DELEGATION_SPECS
from adam.tool_calls.delegation_tools import encode_results, handlers, register_progress
from adam.tool_calls.diagnostic_files import DiagnosticFiles
from adam.tool_calls.discovery_tools import register_discovery
from adam.tool_calls.document_tools import register_documents
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.feedback_tools import SPECS as FEEDBACK_SPECS
from adam.tool_calls.feedback_tools import handlers as feedback_handlers
from adam.tool_calls.isolated_commands import register_isolated_commands
from adam.tool_calls.mcp_tools import register_mcp
from adam.tool_calls.orchestration_tools import register_orchestration
from adam.tool_calls.planning_tools import SPECS as PLANNING_SPECS
from adam.tool_calls.planning_tools import handlers as planning_handlers
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.proposal_tools import register_review as register_proposal_review
from adam.tool_calls.proposal_tools import register_submission
from adam.tool_calls.review_tools import SPECS as REVIEW_SPECS
from adam.tool_calls.review_tools import handlers as review_handlers
from adam.tool_calls.session_tools import SessionTools
from adam.tool_calls.skill_tools import register_skills
from adam.tool_calls.terminal_sessions import TerminalSessions
from adam.tool_calls.terminal_tools import TerminalTools
from adam.tool_calls.trigger_tools import TriggerTools

ChildClientFactory = Callable[[CancellationToken], AbstractContextManager[ModelClient]]


def build_application(
    config: AppConfig,
    store: SessionStore,
    client: ModelClient,
    workspace: Path,
    *,
    confirm_command: Callable[[CommandRequest], bool] | None = None,
    confirm_edit: Callable[[FileChangePlan], bool] | None = None,
    confirm_terminal: Callable[[TerminalRequest], bool] | None = None,
    on_tool: Callable[[str], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
    on_plan: Callable[[PlanReport], None] | None = None,
    child_client_factory: ChildClientFactory | None = None,
    decorate_tools: Callable[[ToolRunner], ToolRunner] | None = None,
    observe_conversation: Callable[[Conversation], None] | None = None,
    verifier: TaskVerifier | None = None,
    live_input: bool = False,
    cancel: CancellationToken | None = None,
) -> AgentApplication:
    """将已打开的依赖组装为应用，不接管调用方的主存储或主模型资源。

    config 只在此拆分；模型密钥仅传给模型适配器，工具只得到权限和资源限制。
    on_tool 接收工具名称；标签、颜色和输出位置由界面决定。
    child_client_factory 在工作线程中创建独立模型资源；未提供时不启用委派。
    decorate_tools 供宿主显式装饰主轮次工具边界，例如录制；不从配置或模型加载执行代码。
    observe_conversation 在组装完成后安装宿主观察接口；不从配置或模型加载回调。
    verifier仅供宿主显式选择可信验收器，不继承给子Agent。
    live_input显式启用运行中输入；cancel须与主HTTP适配器共享，默认聊天保持原路径。
    启用子任务或 MCP 的调用方须在释放主会话锁前调用 application.close；
    组装失败会回收已创建资源，主模型与主存储仍由调用方持有。
    """
    with ExitStack() as resources:
        modes = ModeController()
        main_cancel = (cancel or CancellationToken()) if live_input else None
        inputs = RunningInputs(store.root / "_inputs", main_cancel) if main_cancel else None
        documents_dir = config.documents_dir or store.root / "_documents"
        knowledge_dir = config.knowledge_dir or store.root / "_knowledge"
        checkpoints_dir = store.root / "_checkpoints"
        artifacts_dir = store.root / "_artifacts"
        mcp_media_dir = store.root / "_mcp_media"
        background_dir = store.root / "_background"
        protected_dirs = (
            documents_dir,
            checkpoints_dir,
            artifacts_dir,
            knowledge_dir,
            mcp_media_dir,
            background_dir,
            store.root / "_inputs",
            store.root / "_triggers",
            store.root / "_terminals",
            store.root / "_subagents",
        )
        artifacts = _artifact_tools(
            config.artifacts, store, artifacts_dir, config.tool_limits.max_result_chars
        )
        document_config = replace(
            config.documents,
            roots=tuple(
                path if path.is_absolute() else workspace.absolute() / path
                for path in config.documents.roots
            ),
        )
        registry = create_tool_registry(
            workspace,
            config.permissions,
            config.tool_limits,
            confirm_command=confirm_command,
            confirm_edit=confirm_edit,
            protected_dirs=protected_dirs,
            command_output_bytes=config.artifacts.max_bytes if config.artifacts.enabled else None,
            cancel=main_cancel,
        )
        background_service = BackgroundService(
            background_dir,
            BackgroundJobs(CommandTools(workspace, config.tool_limits, confirm_command)),
        )
        resources.callback(background_service.close)
        background = BackgroundTools(background_service.start, background_service.query, store)
        registry = background.register(registry)
        terminal_service = TerminalService(
            store.root / "_terminals",
            TerminalSessions(CommandTools(workspace, config.tool_limits), confirm_terminal),
        )
        resources.callback(terminal_service.close)
        terminal_tools = TerminalTools(
            terminal_service.start,
            terminal_service.send,
            terminal_service.query,
            terminal_service.stop,
            store,
        )
        registry = terminal_tools.register(registry)
        triggers = LocalTriggers(store.root / "_triggers", background_service.query)
        resources.callback(triggers.close)
        trigger_tools = TriggerTools(triggers.dispatch, store) if inputs else None
        if trigger_tools:
            registry = trigger_tools.register(registry)

        def input_queue(turn_id: str) -> EventQueue | None:
            """仅在协调线程安全点观察本地条件，再交给原输入消费与持久化流程。"""
            queue = inputs.queue(turn_id) if inputs else None
            owner = store.active_session_id
            if queue is not None and owner is not None:
                triggers.poll(owner, queue, time.time())
            return queue

        if config.command_isolation.enabled:
            registry = register_isolated_commands(
                registry,
                IsolatedCommands(config.command_isolation.distribution),
                PathPolicy(
                    workspace,
                    config.permissions,
                    protected_dirs=(
                        *protected_dirs,
                        config.memories_dir or store.root / "_memories",
                    ),
                ),
                config.command_isolation.distribution,
                confirm=confirm_command,
            )
        registry = register_documents(
            registry,
            workspace,
            document_config,
            documents_dir,
            config.permissions,
            (
                checkpoints_dir,
                artifacts_dir,
                mcp_media_dir,
                background_dir,
                store.root / "_inputs",
                store.root / "_triggers",
                store.root / "_terminals",
                store.root / "_subagents",
            ),
            knowledge_dir=knowledge_dir,
        )
        registry, skills = register_skills(
            registry,
            workspace,
            config.permissions,
            enabled=config.skills.enabled,
            user_dir=config.skills.user_dir,
            protected_dirs=protected_dirs,
        )
        registry = register_mcp(
            registry, config.mcp, workspace, resources, media_store=MCPMediaStore(mcp_media_dir)
        )
        if artifacts:
            registry = artifacts.register(registry)
        if config.session_search.enabled:
            registry = SessionTools(
                store,
                config.session_search.allowed_session_ids,
                config.tool_limits.max_result_chars,
            ).register(registry)
        manager = None
        if child_client_factory:
            manager = SubagentManager(
                store,
                workspace,
                config.permissions,
                config.subagents,
                config.agent.max_session_tokens,
                config.tool_limits.max_result_chars,
                partial(
                    _open_child,
                    config,
                    child_client_factory,
                    config.memories_dir or store.root / "_memories",
                    document_config,
                    documents_dir,
                    checkpoints_dir,
                    artifacts_dir,
                    knowledge_dir,
                    mcp_media_dir,
                    on_notice or (lambda _: None),
                ),
                on_notice or (lambda _: None),
                mode_provider=modes.current,
            )
            resources.callback(manager.close)
            registry = register_proposal_review(
                registry,
                ProposalReview(
                    store,
                    manager,
                    EditTools(
                        PathPolicy(workspace, config.permissions, protected_dirs=protected_dirs),
                        config.tool_limits,
                        confirm_edit,
                    ),
                    config.tool_limits.max_result_chars,
                ),
            )
            registry = registry.extended(
                DELEGATION_SPECS,
                handlers(manager),
                {"wait_agents": encode_results},
                capabilities=dict.fromkeys(DELEGATION_SPECS, ToolCapability.DELEGATE),
                groups=dict.fromkeys(DELEGATION_SPECS, "agents"),
            )
        planner = (
            PlanningService(store, config.planning, on_change=on_plan, notify=on_notice)
            if config.planning.enabled
            else None
        )
        if planner:
            registry = registry.extended(
                PLANNING_SPECS,
                planning_handlers(planner),
                capabilities=dict.fromkeys(PLANNING_SPECS, ToolCapability.SESSION_STATE),
                groups=dict.fromkeys(PLANNING_SPECS, "planning"),
            )

        reflector = (
            ReflectionService(store, config.reflection, config.context, on_notice)
            if config.reflection.enabled
            else None
        )
        if reflector:
            registry = registry.extended(
                FEEDBACK_SPECS,
                feedback_handlers(reflector),
                capabilities=dict.fromkeys(FEEDBACK_SPECS, ToolCapability.SESSION_STATE),
                groups=dict.fromkeys(FEEDBACK_SPECS, "reflection"),
            )

        reviewer = (
            ReviewService(
                store,
                manager,
                PathPolicy(workspace, config.permissions, protected_dirs=protected_dirs),
                config.tool_limits,
                config.review,
                on_notice or (lambda _: None),
            )
            if manager and config.review.enabled
            else None
        )
        if reviewer:
            registry = registry.extended(
                REVIEW_SPECS,
                review_handlers(reviewer),
                capabilities=dict.fromkeys(REVIEW_SPECS, ToolCapability.DELEGATE),
                groups=dict.fromkeys(REVIEW_SPECS, "review"),
                path_selectors={
                    "request_review": lambda args: tuple(
                        Path(path) for path in args["paths"] if isinstance(path, str)
                    )
                },
            )

        rules = (
            _project_rules(
                workspace,
                config.permissions,
                (*protected_dirs, config.memories_dir or store.root / "_memories"),
            )
            if config.rules.enabled
            else None
        )
        if rules:
            registry = registry.guarded(rules.check)
        if config.orchestration.enabled:
            # 闭包在动作执行时才取得完成组装的原执行器，不能改用无日志的注册表桥。
            registry = register_orchestration(
                registry,
                IsolatedOrchestrator(WslBubblewrap(config.orchestration.distribution)),
                lambda name, args: executor.execute_nested(name, args),
            )
        registry = registry.with_mode(modes.current)
        registry, discovery = register_discovery(
            registry,
            config.tool_discovery,
            modes.current,
            embedding_backend=FastEmbedBackend(
                documents_dir / "models", config.tool_discovery.embedding_model
            )
            if config.tool_discovery.ranking == "semantic"
            else None,
        )

        def bind_source(link: dict[str, str]) -> None:
            """把同一执行来源交给各协调服务，不覆盖原子任务绑定。"""
            background.bind_source(link)
            terminal_tools.bind_source(link)
            if trigger_tools:
                trigger_tools.bind_source(link)
            if manager:
                manager.bind_source(link)
            if planner:
                planner.bind_source(link)
            if reflector:
                reflector.bind_source(link)
            if reviewer:
                reviewer.bind_source(link)

        executor = ToolExecutor(
            store,
            registry,
            on_tool or (lambda _: None),
            before_call=bind_source,
            cancel=main_cancel,
            discovery=discovery,
            no_progress=config.no_progress,
            after_call=planner.publish if planner else None,
            check_call=reviewer.check_call if reviewer else None,
            hooks=HookRunner(config.hooks, workspace) if config.hooks.enabled else None,
            artifacts=artifacts,
            on_notice=on_notice,
        )
        memories = MemoryService(
            MemoryStore(config.memories_dir or store.root / "_memories"),
            workspace,
            config.memory,
            semantic=SemanticMemoryRanker(
                FastEmbedBackend(documents_dir / "models", config.memory.embedding_model),
                min_score=config.memory.semantic_min_score,
                max_records=config.memory.max_semantic_records,
            )
            if config.memory.method == "semantic"
            else None,
        )
        conversation = Conversation(
            store,
            client,
            config.system_prompt,
            decorate_tools(executor) if decorate_tools else executor,
            config.agent,
            verifier=verifier,
            model=config.llm.model,
            cancel=main_cancel,
            on_turn_started=(lambda tid: inputs.begin(store.active_session_id, tid))
            if inputs
            else None,
            input_provider=input_queue if inputs else None,
            context_config=config.context,
            notify=on_notice,
            accounting=manager.account if manager else None,
            delegation=manager,
            planning=planner,
            reflection=reflector,
            review=reviewer,
            memory_provider=memories.select,
            mode_provider=modes.current,
            skills=skills.summaries() if skills else (),
            rules=rules,
            diagnostic_reader=DiagnosticContextReader(
                store,
                DiagnosticFiles(
                    PathPolicy(workspace, config.permissions, protected_dirs=protected_dirs),
                    config.tool_limits.max_text_file_bytes,
                ),
            )
            if config.hooks.enabled and config.hooks.after_tool is not None
            else None,
            state_reader=RuntimeStateReader(
                store,
                workspace,
                config.agent.max_session_tokens,
                modes.current,
                executor.failure_state,
                tokens=(lambda sid: manager.account(sid).snapshot()) if manager else None,
                pending=manager.pending_agents if manager else None,
            ),
        )

        def recover_attached(session_id: str) -> None:
            """主会话锁已取得后恢复附属记录；即使关闭live-input也不遗漏旧输入。"""
            background_service.recover(session_id)
            triggers.recover(session_id)
            terminal_service.recover(session_id)
            if (store.root / "_inputs" / session_id).exists():
                recover_inputs(
                    store.root / "_inputs",
                    session_id,
                    {turn.turn_id for turn in store.load_turns(session_id)},
                )

        def interrupt_processes() -> None:
            """取消当前主会话的后台任务和持久终端，不影响其他会话。"""
            try:
                background_service.cancel_owner(store.active_session_id)
            finally:
                terminal_service.cancel_owner(store.active_session_id)

        if observe_conversation:
            observe_conversation(conversation)
        return AgentApplication(
            conversation,
            store,
            client,
            memories,
            close_resources=resources.pop_all().close,
            running_inputs=inputs,
            on_interrupt=interrupt_processes,
            on_session_selected=recover_attached,
            skills=skills,
            modes=modes,
            undo=UndoService(
                store,
                EditTools(
                    PathPolicy(
                        workspace,
                        config.permissions,
                        protected_dirs=protected_dirs,
                    ),
                    config.tool_limits,
                    confirm_edit,
                ),
                modes.current,
            ),
        )


@contextmanager
def _open_child(
    config: AppConfig,
    client_factory: ChildClientFactory,
    memories_dir: Path,
    document_config: DocumentConfig,
    documents_dir: Path,
    checkpoints_dir: Path,
    artifacts_dir: Path,
    knowledge_dir: Path,
    mcp_media_dir: Path,
    notify: Callable[[str], None],
    root: Path,
    agent: ChildAgent,
    run: ChildRun,
    cancel: CancellationToken,
    budget: SharedTokenBudget,
    started: Callable[[str], None],
    memory: MemorySelection,
    publish_progress: Callable[[str, str], dict[str, Any]],
) -> Iterator[Conversation]:
    """在子线程中管理独立资源，继承派发时的模式与主轮次记忆快照。

    不传主会话锁、HTTP 或审批回调；工具禁止直接读取记忆、文档缓存、知识记录、检查点和附件（含 MCP 媒体）。
    知识仅经当前授权来源的查询接口读取；子任务不能保存候选、审查或发布。
    文档源固定为主应用配置；检索仍按子任务自身的读取权限逐个检查。
    Hooks 继承用户配置，使用子任务取消令牌；后置故障提示带子任务 ID。
    辅助子任务可保存有读取依据的提案；审查者不注册此工具，所有子任务均不能应用。
    """
    mode = ExecutionMode(run.execution_mode)
    with SessionStore(root) as store, client_factory(cancel) as client:
        store.select_session(agent.agent_id)
        account = SessionAccount(store, agent.agent_id, budget)
        # 子任务按所属主会话分组；主/子短 ID 即使相同，也不会共享附件命名空间。
        artifacts = _artifact_tools(
            config.artifacts,
            store,
            artifacts_dir / "children" / root.parent.name,
            config.tool_limits.max_result_chars,
        )
        registry = create_tool_registry(
            Path(agent.workspace),
            PermissionConfig(additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)),
            config.tool_limits,
            delegated=True,
            protected_dirs=(
                memories_dir,
                documents_dir,
                checkpoints_dir,
                artifacts_dir,
                knowledge_dir,
                mcp_media_dir,
                checkpoints_dir.parent / "_background",
                checkpoints_dir.parent / "_triggers",
                checkpoints_dir.parent / "_terminals",
                checkpoints_dir.parent / "_inputs",
                checkpoints_dir.parent / "_subagents",
            ),
        )
        registry = register_documents(
            registry,
            Path(agent.workspace),
            document_config,
            documents_dir,
            PermissionConfig(additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)),
            (
                memories_dir,
                checkpoints_dir,
                artifacts_dir,
                mcp_media_dir,
                checkpoints_dir.parent / "_background",
                checkpoints_dir.parent / "_triggers",
                checkpoints_dir.parent / "_terminals",
                checkpoints_dir.parent / "_inputs",
                checkpoints_dir.parent / "_subagents",
            ),
            check_cancel=cancel.check,
            knowledge_dir=knowledge_dir,
            allow_knowledge_proposals=False,
        )
        registry, skills = register_skills(
            registry,
            Path(agent.workspace),
            PermissionConfig(additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)),
            enabled=config.skills.enabled,
            user_dir=config.skills.user_dir,
            protected_dirs=(
                memories_dir,
                documents_dir,
                checkpoints_dir,
                artifacts_dir,
                knowledge_dir,
                mcp_media_dir,
                checkpoints_dir.parent / "_background",
                checkpoints_dir.parent / "_triggers",
                checkpoints_dir.parent / "_terminals",
                checkpoints_dir.parent / "_inputs",
                checkpoints_dir.parent / "_subagents",
            ),
        )
        rules = (
            _project_rules(
                Path(agent.workspace),
                PermissionConfig(
                    additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)
                ),
                (
                    memories_dir,
                    documents_dir,
                    checkpoints_dir,
                    artifacts_dir,
                    knowledge_dir,
                    mcp_media_dir,
                    checkpoints_dir.parent / "_background",
                    checkpoints_dir.parent / "_triggers",
                    checkpoints_dir.parent / "_terminals",
                    checkpoints_dir.parent / "_inputs",
                    checkpoints_dir.parent / "_subagents",
                ),
            )
            if config.rules.enabled
            else None
        )
        if rules:
            registry = registry.guarded(rules.check)
        if artifacts:
            registry = artifacts.register(registry)
        submission = None
        if agent.role == "helper":
            submission = ProposalSubmission(
                store,
                ProposalOwner(root.parent.name, run.parent_turn_id, agent.agent_id, run.run_id),
                PathPolicy(
                    Path(agent.workspace),
                    PermissionConfig(
                        additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)
                    ),
                    protected_dirs=(
                        memories_dir,
                        documents_dir,
                        checkpoints_dir,
                        artifacts_dir,
                        knowledge_dir,
                        mcp_media_dir,
                        checkpoints_dir.parent / "_background",
                        checkpoints_dir.parent / "_triggers",
                        checkpoints_dir.parent / "_terminals",
                        checkpoints_dir.parent / "_inputs",
                        checkpoints_dir.parent / "_subagents",
                    ),
                ),
                config.tool_limits,
                cancel,
            )
            registry = register_submission(registry, submission)
            registry = register_progress(registry, publish_progress)
        if config.orchestration.enabled:
            registry = register_orchestration(
                registry,
                IsolatedOrchestrator(WslBubblewrap(config.orchestration.distribution)),
                lambda name, args: executor.execute_nested(name, args),
                cancel=cancel,
            )
        registry = registry.with_mode(lambda: mode)
        registry, discovery = register_discovery(
            registry,
            config.tool_discovery,
            lambda: mode,
            embedding_backend=FastEmbedBackend(
                documents_dir / "models", config.tool_discovery.embedding_model
            )
            if config.tool_discovery.ranking == "semantic"
            else None,
        )
        executor = ToolExecutor(
            store,
            registry,
            lambda _: None,
            cancel=cancel,
            before_call=submission.bind_source if submission else None,
            discovery=discovery,
            no_progress=config.no_progress,
            hooks=(
                HookRunner(config.hooks, Path(agent.workspace), cancel)
                if config.hooks.enabled
                else None
            ),
            on_notice=lambda message: notify(f"子 Agent> {agent.agent_id} {message}"),
            artifacts=artifacts,
        )
        conversation = Conversation(
            store,
            client,
            "",
            executor,
            config.agent,
            model=run.model,
            context_config=config.context,
            accounting=lambda _: account,
            cancel=cancel,
            on_turn_started=started,
            memory_provider=lambda _query, _capacity: memory,
            mode_provider=lambda: mode,
            skills=skills.summaries() if skills else (),
            rules=rules,
            diagnostic_reader=DiagnosticContextReader(
                store,
                DiagnosticFiles(
                    PathPolicy(
                        Path(agent.workspace),
                        PermissionConfig(
                            additional_read_dirs=tuple(Path(p) for p in agent.read_dirs)
                        ),
                        protected_dirs=(
                            memories_dir,
                            documents_dir,
                            checkpoints_dir,
                            artifacts_dir,
                            knowledge_dir,
                            mcp_media_dir,
                            checkpoints_dir.parent / "_background",
                            checkpoints_dir.parent / "_triggers",
                            checkpoints_dir.parent / "_terminals",
                            checkpoints_dir.parent / "_inputs",
                            checkpoints_dir.parent / "_subagents",
                        ),
                    ),
                    config.tool_limits.max_text_file_bytes,
                    cancel,
                ),
            )
            if config.hooks.enabled and config.hooks.after_tool is not None
            else None,
            state_reader=RuntimeStateReader(
                store,
                Path(agent.workspace),
                config.agent.max_session_tokens,
                lambda: mode,
                executor.failure_state,
                tokens=lambda _: account.snapshot(),
            ),
        )
        conversation.resume(agent.agent_id)
        yield conversation


def _artifact_tools(
    config: ArtifactConfig, store: SessionStore, root: Path, max_result_chars: int
) -> ToolArtifacts | None:
    """组装每个应用独立的附件入口；关闭时不创建存储或注册读取工具。"""
    if not config.enabled:
        return None
    return ToolArtifacts(
        store,
        ArtifactStore(
            root,
            max_bytes=config.max_bytes,
            max_session_bytes=config.max_session_bytes,
            max_items=config.max_items,
            max_read_chars=config.max_read_chars,
        ),
        max_result_chars,
    )


def _project_rules(
    workspace: Path, permissions: PermissionConfig, protected_dirs: tuple[Path, ...]
) -> RuleTracker:
    """为每个主/子应用创建独立规则状态，沿用读取授权与保留目录保护。"""
    policy = PathPolicy(workspace, permissions, protected_dirs=protected_dirs)
    return RuleTracker(RuleCatalog(workspace, partial(policy.resolve, must_exist=False)))


@contextmanager
def open_application(
    config_path: Path,
    llm_path: Path | None,
    workspace: Path,
    *,
    confirm_command: Callable[[CommandRequest], bool] | None = None,
    confirm_edit: Callable[[FileChangePlan], bool] | None = None,
    confirm_terminal: Callable[[TerminalRequest], bool] | None = None,
    on_tool: Callable[[str], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
    on_plan: Callable[[PlanReport], None] | None = None,
    live_input: bool = False,
) -> Iterator[AgentApplication]:
    """从配置打开应用；配置错误不会创建 HTTP 客户端或会话目录。

    yield 期间拥有存储与 HTTP 资源；启动失败、退出、取消或界面异常都释放它们。
    先停止并回收子任务与 MCP 服务，再释放主会话锁；已知业务错误按 AgentError 契约传播，
    不把界面故障误判成存储错误。
    """
    config = load_config(config_path, llm_path)
    main_cancel = CancellationToken() if live_input else None

    @contextmanager
    def child_client(cancel: CancellationToken) -> Iterator[ModelClient]:
        """工厂在子线程调用，每次运行独立拥有事件循环和连接池。"""
        with httpx.Client(transport=InterruptibleTransport(cancel), follow_redirects=False) as http:
            yield LLMClient(config.llm, http)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=InterruptibleTransport(main_cancel), follow_redirects=False) as http,
    ):
        application = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_command=confirm_command,
            confirm_edit=confirm_edit,
            confirm_terminal=confirm_terminal,
            on_tool=on_tool,
            on_notice=on_notice,
            on_plan=on_plan,
            child_client_factory=child_client,
            live_input=live_input,
            cancel=main_cancel,
        )
        try:
            yield application
        except BaseException as error:
            try:
                application.close()
            except Exception:
                error.add_note("应用资源清理时发生错误；原始失败保留，请检查会话记录。")
            raise
        else:
            application.close()
