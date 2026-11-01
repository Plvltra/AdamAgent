"""按次创建网页资源；不向会话或工具调度线程泄漏事件循环。"""

from collections.abc import Iterator
from contextlib import contextmanager

from adam.web.adapters.http import HttpPageTransport
from adam.web.adapters.wikipedia import WikipediaSearch
from adam.web.models import FetchLimits, WebError
from adam.web.policy import NetworkPolicy
from adam.web.search_models import SearchBackend, SearchLimits
from adam.web.service import WebReader


@contextmanager
def open_reader(limits: FetchLimits, policy: NetworkPolicy) -> Iterator[WebReader]:
    """一次调用独占网络资源；构造/执行失败和取消均关闭，清理失败不覆盖原故障。"""
    transport = HttpPageTransport(policy, limits)
    try:
        yield WebReader(transport, policy, limits)
    except BaseException as error:
        try:
            transport.close()
        except Exception:
            error.add_note("网页资源清理失败；原始错误保留，未自动重发请求。")
        raise
    else:
        transport.close()


@contextmanager
def open_search(
    backend: str | None, limits: SearchLimits, policy: NetworkPolicy
) -> Iterator[SearchBackend]:
    """显式选择搜索后端；未配置不创建资源，取消/失败清理且不重试。"""
    if backend is None:
        raise WebError("not_configured", "搜索后端未配置；请显式选择 wikipedia。")
    if backend != "wikipedia":
        raise WebError("unsupported_backend", "搜索后端不受支持。")
    transport = HttpPageTransport(
        policy,
        FetchLimits(max_bytes=limits.max_bytes, timeout_seconds=limits.timeout_seconds),
        content_types=frozenset({"application/json"}),
        user_agent="AdamAgentPractice/0.0.1 (read-only encyclopedia search)",
    )
    try:
        yield WikipediaSearch(transport, policy, limits)
    except BaseException as error:
        try:
            transport.close()
        except Exception:
            error.add_note("搜索资源清理失败；原始错误保留，未自动重发请求。")
        raise
    else:
        transport.close()
