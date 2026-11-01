"""显式本地 llama.cpp 视觉连接；不读取远端密钥或更改默认聊天配置。"""

import math
import re
from dataclasses import dataclass

from adam.contracts.errors import ConfigError


@dataclass(frozen=True)
class LocalVisionConfig:
    """宿主确认的本地视觉模型及地址；模型别名必须与服务器 --alias 一致。"""

    model: str
    endpoint: str = "http://127.0.0.1:8087/v1"
    timeout_seconds: float = 120
    stream: bool = True

    def __post_init__(self) -> None:
        """只允许字面 IPv4 回环及高位端口，拒绝代理地址、重定向目标和隐含外传。"""
        match = (
            re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/v1", self.endpoint)
            if isinstance(self.endpoint, str)
            else None
        )
        if match is None or not 1024 <= int(match[1]) <= 65535:
            raise ConfigError("本地视觉 endpoint 必须是 http://127.0.0.1:高位端口/v1。")
        if (
            not isinstance(self.model, str)
            or re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", self.model) is None
        ):
            raise ConfigError("本地视觉 model 必须是有界 ASCII 模型别名。")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not 0 < self.timeout_seconds <= 600
            or not math.isfinite(self.timeout_seconds)
            or type(self.stream) is not bool
        ):
            raise ConfigError("本地视觉超时必须在 (0, 600] 秒内，stream 必须为布尔值。")
