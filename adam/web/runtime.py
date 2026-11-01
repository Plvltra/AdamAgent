"""按次创建网页资源；不向会话或工具调度线程泄漏事件循环。"""

from collections.abc import Iterator
from contextlib import contextmanager

from adam.web.adapters.http import HttpPageTransport
from adam.web.models import FetchLimits
from adam.web.policy import NetworkPolicy
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
