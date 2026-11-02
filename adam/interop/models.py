"""公共文本互操作结果，不暴露 HTTP 或供应商 SDK 类型。"""

from dataclasses import dataclass

from adam.contracts.errors import AgentError


class InteropError(AgentError):
    """协议失败；调用方不能据此推断远端没有执行。"""


@dataclass(frozen=True)
class AgentCard:
    """验证了地址与版本的服务声明，不代表身份认证。"""

    name: str
    url: str
    skills: tuple[str, ...]
    protocol_version: str = "0.3.0"


@dataclass(frozen=True)
class TextResult:
    """返回消息标识和文本；内容仍是未独立验证的远端声明。"""

    message_id: str
    text: str
