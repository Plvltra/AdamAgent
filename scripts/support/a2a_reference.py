"""固定 a2a-sdk 0.3.0 参考服务；业务无模型或外部副作用，协议由 SDK 实现。"""

import argparse
import asyncio
import json
import os
import socket
import sys
from importlib.metadata import version
from pathlib import Path
from threading import Thread

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Part,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)


class TextExecutor(AgentExecutor):
    """确定性参考业务，用逐次调用记录证明没有重复执行。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """发布真实 Task 和文本产物；wait 任务等待 SDK 的取消通路。"""
        task_id, context_id = context.task_id, context.context_id
        assert task_id and context_id
        text = context.get_user_input()
        with (self.root / "calls.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps({"task_id": task_id, "text": text}) + "\n")
        await event_queue.enqueue_event(
            Task(id=task_id, context_id=context_id, status=TaskStatus(state=TaskState.submitted))
        )
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.start_work()
        if text == "wait":
            await asyncio.sleep(60)
        if text == "fail":
            await updater.failed()
            return
        await updater.add_artifact([Part(root=TextPart(text=text.upper()))])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """交给 SDK 取消运行协程；参考业务等待期间没有外部副作用。"""
        assert context.task_id and context.context_id
        await TaskUpdater(event_queue, context.task_id, context.context_id).update_status(
            TaskState.canceled, final=True
        )


class DelayedQueryHandler(DefaultRequestHandler):
    """只在显式标记存在时延迟一次查询响应；不替换 SDK 协议或任务存储。"""

    async def on_get_task(self, params, context=None):  # noqa: ANN001, ANN201
        marker = self.agent_executor.root / "delay-query"
        if marker.exists():
            marker.unlink()
            await asyncio.sleep(0.3)
        return await super().on_get_task(params, context)


def main() -> None:
    """绑定临时回环端口并发布运行身份，stdin关闭时协作停止服务。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    assert version("a2a-sdk") == "0.3.0"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        card = AgentCard(
            name="Pinned SDK reference",
            description="Deterministic text reference",
            url=origin + "/",
            version="0.1.0",
            protocol_version="0.3.0",
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            capabilities=AgentCapabilities(streaming=False, push_notifications=False),
            skills=[
                AgentSkill(
                    id="text", name="Text", description="Uppercase bounded text", tags=["text"]
                )
            ],
        )
        handler = DelayedQueryHandler(
            agent_executor=TextExecutor(args.root), task_store=InMemoryTaskStore()
        )
        app = A2AStarletteApplication(agent_card=card, http_handler=handler).build()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=1)
        )

        def stop() -> None:
            sys.stdin.readline()
            server.should_exit = True

        Thread(target=stop, daemon=True).start()
        (args.root / "ready.json").write_text(
            json.dumps(
                {
                    "origin": origin,
                    "pid": os.getpid(),
                    "sdk": version("a2a-sdk"),
                    "uvicorn": version("uvicorn"),
                }
            ),
            encoding="utf-8",
        )
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
