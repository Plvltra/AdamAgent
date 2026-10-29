"""将可取消的异步 HTTP 接到同步业务接口；会话存储始终留在调用线程。"""

import asyncio
from collections.abc import Awaitable, Iterator
from contextlib import suppress
from typing import TypeVar

import httpx

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted

_Result = TypeVar("_Result")


class InterruptibleTransport(httpx.BaseTransport):
    """由单个调用线程拥有的 HTTP 适配器；Ctrl+C 或停止令牌取消当前 IO。

    事件循环与连接池由此对象拥有，随 httpx.Client.close 释放；子 Agent 必须
    在自己的线程创建独立实例，不跨线程共享，也不隐藏重试。调用方不得在已有 asyncio 循环内嵌套使用。
    """

    def __init__(self, cancel: CancellationToken | None = None) -> None:
        self.cancel = cancel
        self._runner = asyncio.Runner()
        self._transport = httpx.AsyncHTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """取得响应头；响应正文仍逐块读取，取消时由异步传输关闭当前连接。"""
        response = self.run(self._transport.handle_async_request(request))
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_ResponseStream(self, response),
            extensions=response.extensions,
        )

    def run(self, operation: Awaitable[_Result], *, cleanup: bool = False) -> _Result:
        """等待 IO 并检查停止信号；资源清理不能被已经置位的令牌再次打断。"""
        try:
            return self._runner.run(_wait_for_io(operation, None if cleanup else self.cancel))
        except asyncio.CancelledError:
            raise RequestInterrupted() from None

    def close(self) -> None:
        """关闭连接池与事件循环；不保留未完成的网络请求。"""
        try:
            self.run(self._transport.aclose(), cleanup=True)
        finally:
            self._runner.close()


class _ResponseStream(httpx.SyncByteStream):
    """只桥接异步字节流，SSE 解析和业务状态由上层处理。"""

    def __init__(self, transport: InterruptibleTransport, response: httpx.Response) -> None:
        self.transport = transport
        self.response = response

    def __iter__(self) -> Iterator[bytes]:
        iterator = self.response.aiter_raw()
        while True:
            try:
                yield self.transport.run(anext(iterator))
            except StopAsyncIteration:
                return

    def close(self) -> None:
        self.transport.run(self.response.aclose(), cleanup=True)


async def _wait_for_io(
    operation: Awaitable[_Result], cancel: CancellationToken | None = None
) -> _Result:
    """取消等待时也取消真正的 IO；短周期等待不改变网络超时或重新发送请求。"""
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            if cancel is not None and cancel.cancelled:
                raise asyncio.CancelledError
            await asyncio.wait({task}, timeout=0.1)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
