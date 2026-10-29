"""已有业务场景的依赖组装；保持场景输入和断言，生产 CLI 只接受应用接口。"""

from pathlib import Path

from adam.application.conversation import Conversation
from adam.bootstrap import build_application
from adam.cli.main import chat, confirm_command, confirm_edit
from adam.config.settings import (
    AgentConfig,
    AppConfig,
    ContextConfig,
    PermissionConfig,
    ReflectionConfig,
    SkillsConfig,
    ToolLimits,
)
from adam.tool_calls.executor import ToolExecutor
from adam.tool_calls.factory import create_tool_registry


def build_conversation(
    store, client, prompt, registry=None, limits=None, notify=None, *, context_config=None
):
    """为直接验证执行流程的场景注入依赖；测试明确提供客户端的默认模型。"""
    tools = registry or create_tool_registry(Path.cwd())
    return Conversation(
        store,
        client,
        prompt,
        ToolExecutor(
            store, tools, (lambda name: notify(f"工具> {name}")) if notify else (lambda _: None)
        ),
        limits or AgentConfig(),
        model=client.config.model,
        context_config=context_config,
        notify=notify,
    )


def run_chat(
    client,
    system_prompt,
    store,
    read=input,
    write=print,
    resume_id=None,
    workspace=None,
    permissions=None,
    tool_limits=None,
    agent_config=None,
    context_config=None,
):
    """经生产组装逻辑运行原有交互场景；资源仍由测试的 with 块管理。"""
    config = AppConfig(
        client.config,
        store.root,
        system_prompt,
        agent_config or AgentConfig(),
        permissions or PermissionConfig(),
        tool_limits or ToolLimits(),
        context_config or ContextConfig(),
        reflection=ReflectionConfig(enabled=False),
        # 通用聊天场景固定关闭 Skill；E6 专门场景启用临时目录中的指导。
        skills=SkillsConfig(enabled=False),
    )
    application = build_application(
        config,
        store,
        client,
        workspace or Path.cwd(),
        confirm_command=lambda command: confirm_command(command, read, write),
        confirm_edit=lambda plan: confirm_edit(plan, read, write),
        on_tool=lambda name: write(f"工具> {name}"),
        on_notice=write,
    )
    chat(application, read=read, write=write, resume_id=resume_id)
