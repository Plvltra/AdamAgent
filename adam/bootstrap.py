"""唯一的应用组装入口；加载配置，创建依赖并在退出或启动失败时释放资源。"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import partial
from pathlib import Path

import httpx

from adam.application.conversation import Conversation
from adam.application.facade import AgentApplication
from adam.application.memories import MemoryService
from adam.application.subagents.account import SessionAccount
from adam.application.subagents.budget import SharedTokenBudget
from adam.application.subagents.manager import SubagentManager
from adam.config.loader import load_config
from adam.config.settings import AppConfig, PermissionConfig
from adam.contracts.cancellation import CancellationToken
from adam.contracts.delegation import ChildAgent, ChildRun
from adam.contracts.memory import MemorySelection
from adam.contracts.models import CommandRequest, EditPlan
from adam.contracts.ports import ModelClient
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.memory.store import MemoryStore
from adam.session.storage import SessionStore
from adam.tool_calls.delegation_tools import SPECS as DELEGATION_SPECS
from adam.tool_calls.delegation_tools import encode_results, handlers
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry

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
    child_client_factory: ChildClientFactory | None = None,
) -> AgentApplication:
    """将已打开的依赖组装为应用，不接管调用方的主存储或主模型资源。

    config 只在此拆分；模型密钥仅传给模型适配器，工具只得到权限和资源限制。
    on_tool 接收工具名称；标签、颜色和输出位置由界面决定。
    child_client_factory 在工作线程中创建独立模型资源；未提供时不启用委派。
    使用子任务的调用方须在释放主会话锁前调用 application.close。
    """
    registry = create_tool_registry(
        workspace,
        config.permissions,
        config.tool_limits,
        confirm_command=confirm_command,
        confirm_edit=confirm_edit,
    )
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
            ),
            on_notice or (lambda _: None),
        )
        registry = registry.extended(
            DELEGATION_SPECS,
            handlers(manager),
            {"wait_agents": encode_results},
        )
    executor = ToolExecutor(
        store,
        registry,
        on_tool or (lambda _: None),
        before_call=manager.bind_source if manager else None,
    )
    memories = MemoryService(
        MemoryStore(config.memories_dir or store.root / "_memories"), workspace, config.memory
    )
    conversation = Conversation(
        store,
        client,
        config.system_prompt,
        executor,
        config.agent,
        model=config.llm.model,
        context_config=config.context,
        notify=on_notice,
        accounting=manager.account if manager else None,
        delegation=manager,
        memory_provider=memories.select,
    )
    return AgentApplication(conversation, store, client, memories)


@contextmanager
def _open_child(
    config: AppConfig,
    client_factory: ChildClientFactory,
    memories_dir: Path,
    root: Path,
    agent: ChildAgent,
    run: ChildRun,
    cancel: CancellationToken,
    budget: SharedTokenBudget,
    started: Callable[[str], None],
    memory: MemorySelection,
) -> Iterator[Conversation]:
    """在子线程中管理独立资源，只继承主轮次记忆快照。

    不传主会话锁、HTTP 或审批回调；工具禁止读取共享记忆目录，避免绕过选择范围。
    """
    with SessionStore(root) as store, client_factory(cancel) as client:
        store.select_session(agent.agent_id)
        account = SessionAccount(store, agent.agent_id, budget)
        registry = create_tool_registry(
            Path(agent.workspace),
            PermissionConfig(additional_read_dirs=tuple(Path(path) for path in agent.read_dirs)),
            config.tool_limits,
            delegated=True,
            protected_dirs=(memories_dir,),
        )
        conversation = Conversation(
            store,
            client,
            "",
            ToolExecutor(store, registry, lambda _: None, cancel=cancel),
            config.agent,
            model=run.model,
            context_config=config.context,
            accounting=lambda _: account,
            cancel=cancel,
            on_turn_started=started,
            memory_provider=lambda _query, _capacity: memory,
        )
        conversation.resume(agent.agent_id)
        yield conversation


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
) -> Iterator[AgentApplication]:
    """从配置打开应用；配置错误不会创建 HTTP 客户端或会话目录。

    yield 期间拥有存储与 HTTP 资源；启动失败、退出、取消或界面异常都释放它们。
    先停止并回收子任务，再释放主会话锁；已知业务错误按 AgentError 契约传播，
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
            child_client_factory=child_client,
        )
        try:
            yield application
        except BaseException as error:
            try:
                application.close()
            except Exception:
                error.add_note("子任务清理时发生错误；原始失败保留，请检查子会话记录。")
            raise
        else:
            application.close()
