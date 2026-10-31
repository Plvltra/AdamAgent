"""评估专用模型边界：保存实际上下文及响应状态，不保存连接配置或 HTTP 头。"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.contracts.errors import AgentError
from adam.contracts.models import Completion, ModelOutput
from adam.contracts.ports import ModelClient


def write_record(path: Path, data: dict[str, Any], *, exclusive: bool = False) -> None:
    """写完并刷盘；中断导致的残缺记录交给报告判为未知，不修补或重发请求。"""
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class CapturedModel:
    """装饰生产模型接口，记录每次输入是否取得有效响应；捕获失败则停止后续调用。"""

    def __init__(self, inner: ModelClient, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.directory.mkdir()
        self.count = 0

    def list_models(self) -> dict[str, str | None]:
        return self.inner.list_models()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        on_output: Callable[[ModelOutput], None] | None = None,
    ) -> Completion:
        """先保存输入，再请求一次；成功/失败分开，未完成捕获不能算模型已看到材料。"""
        self.count += 1
        path = self.directory / f"{self.count:04d}.json"
        record = {"index": self.count, "messages": messages, "state": "started"}
        write_record(path, record, exclusive=True)
        try:
            result = self.inner.complete(
                messages, model=model, tools=tools, max_tokens=max_tokens, on_output=on_output
            )
        except BaseException as error:
            # 不复制异常文字或远端正文，避免把连接秘密写进评估证据。
            record.update(
                state="failed",
                error=error.code.value if isinstance(error, AgentError) else "interrupted_or_local",
            )
            write_record(path, record)
            raise
        record.update(state="succeeded", reply=result.message)
        write_record(path, record)
        return result
