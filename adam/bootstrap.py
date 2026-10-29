"""唯一的应用组装入口；加载配置，创建依赖并在退出或启动失败时释放资源。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from adam.application.conversation import Conversation
from adam.application.facade import AgentApplication
from adam.config.loader import load_config
from adam.config.settings import AppConfig
from adam.contracts.models import CommandRequest, EditPlan
from adam.contracts.ports import ModelClient
from adam.llm.client import LLMClient
from adam.llm.http_transport import InterruptibleTransport
from adam.session.storage import SessionStore
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry


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
) -> AgentApplication:
    """将已打开的依赖组装为应用，不接管调用方的资源。

    config 只在此拆分；模型密钥仅传给模型适配器，工具只得到权限和资源限制。
    on_tool 接收工具名称；标签、颜色和输出位置由界面决定。
    """
    registry = create_tool_registry(
        workspace,
        config.permissions,
        config.tool_limits,
        confirm_command=confirm_command,
        confirm_edit=confirm_edit,
    )
    executor = ToolExecutor(store, registry, on_tool or (lambda _: None))
    conversation = Conversation(
        store,
        client,
        config.system_prompt,
        executor,
        config.agent,
        model=config.llm.model,
        context_config=config.context,
        notify=on_notice,
    )
    return AgentApplication(conversation, store, client)


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
    已知业务错误按 AgentError 契约传播；不把界面故障误判成存储错误。
    """
    config = load_config(config_path, llm_path)
    with (
        SessionStore(config.sessions_dir) as store,
        httpx.Client(transport=InterruptibleTransport(), follow_redirects=False) as http,
    ):
        yield build_application(
            config,
            store,
            LLMClient(config.llm, http),
            workspace,
            confirm_command=confirm_command,
            confirm_edit=confirm_edit,
            on_tool=on_tool,
            on_notice=on_notice,
        )
