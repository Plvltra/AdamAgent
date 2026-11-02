"""Messages 的输入用量含三项，不能直接套用 Chat Completions 的计量字段。"""


def parse_messages_usage(value: object) -> tuple[int, int] | None:
    """返回总输入和输出；缺失或非法计数保持未知，不把缓存缺项猜成零。

    协议依据：https://api-docs.deepseek.com/guides/anthropic_api/
    input_tokens不包含缓存读取/创建量；三项相加才是输入总量。
    """
    if not isinstance(value, dict):
        return None
    counts: list[int] = []
    for name in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    ):
        count = value.get(name)
        if type(count) is not int or count < 0:
            return None
        counts.append(count)
    return sum(counts[:3]), counts[3]
