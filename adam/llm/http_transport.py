"""将可取消的异步 HTTP 接到同步业务接口；会话存储始终留在调用线程。"""

import asyncio
from collections.abc import Awaitable, Iterator
from contextlib import suppress
from typing import TypeVar

import httpx

_Result = TypeVar("_Result")


class InterruptibleTransport(httpx.BaseTransport):
    """供单线程 CLI 使用的 HTTP 适配器；Ctrl+C 取消当前 IO，后续请求仍可复用。

    事件循环与连接池由此对象拥有，随 httpx.Client.close 释放；不在工作线程中
    运行 SessionStore，也不隐藏重试。调用方不得在已有 asyncio 循环内嵌套使用。
    """

    def __init__(self) -> None:
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

    def run(self, operation: Awaitable[_Result]) -> _Result:
        """在同一事件循环等待一次 IO；周期让出控制，保证 Windows 能处理停止信号。"""
        return self._runner.run(_wait_for_io(operation))

    def close(self) -> None:
        """关闭连接池与事件循环；不保留未完成的网络请求。"""
        try:
            self.run(self._transport.aclose())
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
        self.transport.run(self.response.aclose())


async def _wait_for_io(operation: Awaitable[_Result]) -> _Result:
    """取消等待时也取消真正的 IO；短周期等待不改变网络超时或重新发送请求。"""
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
