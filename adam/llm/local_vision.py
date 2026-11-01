"""llama.cpp 的显式单图视觉适配器；复用 Chat Completions 编码、解析和取消边界。"""

import httpx

from adam.config.settings import LLMConfig
from adam.config.vision import LocalVisionConfig
from adam.contracts.model_input import ModelCapabilities
from adam.llm.client import LLMClient
from adam.llm.message_encoding import ChatInputEncoder, MediaReader


class LlamaVisionClient(LLMClient):
    """宿主选定的本地视觉模型；无工具、无历史推理字段，不从模型名猜能力。

    HTTP 资源由调用方持有；生产组装关闭代理、重定向和自动重试。
    本类不启动服务或下载模型；必须在本地先加载匹配的模型及视觉 projector。
    """

    def __init__(
        self, config: LocalVisionConfig, http: httpx.Client, read_media: MediaReader
    ) -> None:
        super().__init__(
            LLMConfig(
                config.endpoint,
                config.model,
                "local-unused",
                timeout_seconds=config.timeout_seconds,
                stream=config.stream,
            ),
            http,
            input_encoder=ChatInputEncoder(
                ModelCapabilities(
                    config.model,
                    ("image/png", "image/jpeg", "image/webp"),
                    max_media_parts=1,
                    tools=False,
                    reasoning=False,
                ),
                read_media,
            ),
        )
