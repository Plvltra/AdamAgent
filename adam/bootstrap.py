"""真实执行应用的组装入口；加载配置，创建依赖并在退出或启动失败时释放资源。"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path

import httpx

from adam.application.conversation import Conversation
from adam.application.facade import AgentApplication
from adam.application.memories import MemoryService
from adam.application.modes import ModeController
from adam.application.planning import PlanningService
from adam.application.reflection import ReflectionService
from adam.application.review import ReviewService
from adam.application.subagents.account import SessionAccount
from adam.application.subagents.budget import SharedTokenBudget
from adam.application.subagents.manager import SubagentManager
from adam.application.undo import UndoService
from adam.config.loader import load_config
from adam.config.settings import AppConfig, ArtifactConfig, DocumentConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.delegation import ChildAgent, ChildRun
from adam.contracts.execution import ExecutionMode, ToolCapability
from adam.contracts.memory import MemorySelection
from adam.contracts.models import CommandRequest, EditPlan
from adam.contracts.planning import PlanReport
from adam.contracts.ports import ModelClient, ToolRunner
from adam.hooks.runner import HookRunner
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.memory.store import MemoryStore
from adam.rules.catalog import RuleCatalog
from adam.rules.tracker import RuleTracker
from adam.session.artifacts import ArtifactStore
from adam.session.storage import SessionStore
from adam.tool_calls.artifact_tools import ToolArtifacts
from adam.tool_calls.delegation_tools import SPECS as DELEGATION_SPECS
from adam.tool_calls.delegation_tools import encode_results, handlers
from adam.tool_calls.document_tools import register_documents
from adam.tool_calls.edit_tools import EditTools
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry
from adam.tool_calls.feedback_tools import SPECS as FEEDBACK_SPECS
from adam.tool_calls.feedback_tools import handlers as feedback_handlers
from adam.tool_calls.mcp_tools import register_mcp
from adam.tool_calls.planning_tools import SPECS as PLANNING_SPECS
from adam.tool_calls.planning_tools import handlers as planning_handlers
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.review_tools import SPECS as REVIEW_SPECS
from adam.tool_calls.review_tools import handlers as review_handlers
from adam.tool_calls.skill_tools import register_skills

ChildClientFactory = Callable[[CancellationToken], AbstractContextManager[ModelClient]]


def build_application(
    config: AppConfig,
    store: SessionStore,
    client: ModelClient,
    workspace: Path,
    *,
    confirm_command: Callable[[CommandRequest], bool] | None = None,
    confirm_edit: Callable[[EditPlan], bool] | None = None,
    on_tool: Callable[[str], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
    on_plan: Callable[[PlanReport], None] | None = None,
    child_client_factory: ChildClientFactory | None = None,
    decorate_tools: Callable[[ToolRunner], ToolRunner] | None = None,
    observe_conversation: Callable[[Conversation], None] | None = None,
) -> AgentApplication:
    """将已打开的依赖组装为应用，不接管调用方的主存储或主模型资源。

    config 只在此拆分；模型密钥仅传给模型适配器，工具只得到权限和资源限制。
    on_tool 接收工具名称；标签、颜色和输出位置由界面决定。
    child_client_factory 在工作线程中创建独立模型资源；未提供时不启用委派。
    decorate_tools 供宿主显式装饰主轮次工具边界，例如录制；不从配置或模型加载执行代码。
    observe_conversation 在组装完成后安装宿主观察接口；不从配置或模型加载回调。
    启用子任务或 MCP 的调用方须在释放主会话锁前调用 application.close；
    组装失败会回收已创建资源，主模型与主存储仍由调用方持有。
    """
    with ExitStack() as resources:
        modes = ModeController()
        documents_dir = config.documents_dir or store.root / "_documents"
        checkpoints_dir = store.root / "_checkpoints"
        artifacts_dir = store.root / "_artifacts"
        protected_dirs = (documents_dir, checkpoints_dir, artifacts_dir)
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
        )
        registry = register_documents(
            registry,
            workspace,
            document_config,
            documents_dir,
            config.permissions,
            (checkpoints_dir, artifacts_dir),
        )
        registry, skills = register_skills(
            registry,
            workspace,
            config.permissions,
            enabled=config.skills.enabled,
            user_dir=config.skills.user_dir,
            protected_dirs=protected_dirs,
        )
        registry = register_mcp(registry, config.mcp, workspace, resources)
        if artifacts:
            registry = artifacts.register(registry)
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
                    on_notice or (lambda _: None),
                ),
                on_notice or (lambda _: None),
                mode_provider=modes.current,
            )
            resources.callback(manager.close)
            registry = registry.extended(
                DELEGATION_SPECS,
                handlers(manager),
                {"wait_agents": encode_results},
                capabilities=dict.fromkeys(DELEGATION_SPECS, ToolCapability.DELEGATE),
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
            )

        reviewer = (
            ReviewService(
                store,
                manager,
                PathPolicy(workspace, config.permissions),
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
        registry = registry.with_mode(modes.current)

        def bind_source(link: dict[str, str]) -> None:
            """把同一执行来源交给各协调服务，不覆盖原子任务绑定。"""
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
            after_call=planner.publish if planner else None,
            check_call=reviewer.check_call if reviewer else None,
            hooks=HookRunner(config.hooks, workspace) if config.hooks.enabled else None,
            artifacts=artifacts,
            on_notice=on_notice,
        )
        memories = MemoryService(
            MemoryStore(config.memories_dir or store.root / "_memories"), workspace, config.memory
        )
        conversation = Conversation(
            store,
            client,
            config.system_prompt,
            decorate_tools(executor) if decorate_tools else executor,
            config.agent,
            model=config.llm.model,
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
        )
        if observe_conversation:
            observe_conversation(conversation)
        return AgentApplication(
            conversation,
            store,
            client,
            memories,
            close_resources=resources.pop_all().close,
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
    notify: Callable[[str], None],
    root: Path,
    agent: ChildAgent,
    run: ChildRun,
    cancel: CancellationToken,
    budget: SharedTokenBudget,
    started: Callable[[str], None],
    memory: MemorySelection,
) -> Iterator[Conversation]:
    """在子线程中管理独立资源，继承派发时的模式与主轮次记忆快照。

    不传主会话锁、HTTP 或审批回调；工具禁止直接读取记忆、文档缓存、检查点和附件，避免绕过范围。
    文档源固定为主应用配置；检索仍按子任务自身的读取权限逐个检查。
    Hooks 继承用户配置，使用子任务取消令牌；后置故障提示带子任务 ID。
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
            protected_dirs=(memories_dir, documents_dir, checkpoints_dir, artifacts_dir),
        )
        registry = register_documents(
            registry,
            Path(agent.workspace),
            document_config,
            documents_dir,
            PermissionConfig(additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)),
            (memories_dir, checkpoints_dir, artifacts_dir),
            check_cancel=cancel.check,
        )
        registry, skills = register_skills(
            registry,
            Path(agent.workspace),
            PermissionConfig(additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)),
            enabled=config.skills.enabled,
            user_dir=config.skills.user_dir,
            protected_dirs=(memories_dir, documents_dir, checkpoints_dir, artifacts_dir),
        )
        rules = (
            _project_rules(
                Path(agent.workspace),
                PermissionConfig(
                    additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)
                ),
                (memories_dir, documents_dir, checkpoints_dir, artifacts_dir),
            )
            if config.rules.enabled
            else None
        )
        if rules:
            registry = registry.guarded(rules.check)
        if artifacts:
            registry = artifacts.register(registry)
        registry = registry.with_mode(lambda: mode)
        conversation = Conversation(
            store,
            client,
            "",
            ToolExecutor(
                store,
                registry,
                lambda _: None,
                cancel=cancel,
                hooks=(
                    HookRunner(config.hooks, Path(agent.workspace), cancel)
                    if config.hooks.enabled
                    else None
                ),
                on_notice=lambda message: notify(f"子 Agent> {agent.agent_id} {message}"),
                artifacts=artifacts,
            ),
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
    confirm_edit: Callable[[EditPlan], bool] | None = None,
    on_tool: Callable[[str], None] | None = None,
    on_notice: Callable[[str], None] | None = None,
    on_plan: Callable[[PlanReport], None] | None = None,
) -> Iterator[AgentApplication]:
    """从配置打开应用；配置错误不会创建 HTTP 客户端或会话目录。

    yield 期间拥有存储与 HTTP 资源；启动失败、退出、取消或界面异常都释放它们。
    先停止并回收子任务与 MCP 服务，再释放主会话锁；已知业务错误按 AgentError 契约传播，
    不把界面故障误判成存储错误。
    """
    config = load_config(config_path, llm_path)

    @contextmanager
    def child_client(cancel: CancellationToken) -> Iterator[ModelClient]:
        """工厂在子线程调用，每次运行独立拥有事件循环和连接池。"""
        with httpx.Client(transport=InterruptibleTransport(cancel), follow_redirects=False) as http:
            yield LLMClient(config.llm, http)

    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=InterruptibleTransport(), follow_redirects=False) as http,
    ):
        application = build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_command=confirm_command,
            confirm_edit=confirm_edit,
            on_tool=on_tool,
            on_notice=on_notice,
            on_plan=on_plan,
            child_client_factory=child_client,
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
