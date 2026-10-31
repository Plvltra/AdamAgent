"""Chat Completions 用量解析；只读远端字段，不以本地估算补填缺失计数。"""

from adam.contracts.usage import CacheUsage


def parse_usage(value: object) -> tuple[int, int] | None:
    """仅接受服务端报告的非负整数用量；缺失或非法值保持未知。"""
    if isinstance(value, dict):
        inputs, outputs = value.get("prompt_tokens"), value.get("completion_tokens")
        if type(inputs) is int and type(outputs) is int and inputs >= 0 and outputs >= 0:
            return inputs, outputs
    return None


def parse_cache_usage(value: object) -> CacheUsage | None:
    """解析 flat hit/miss 及 nested cached_tokens；冲突降级未知，总用量由 parse_usage 独立保留。

    只得到命中数时保留 miss=None；不通过相减制造服务端未报告的数字。
    协议依据：https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/
    """
    usage = parse_usage(value)
    if usage is None or not isinstance(value, dict):
        return None
    detail = value.get("prompt_tokens_details")
    alias = detail.get("cached_tokens") if isinstance(detail, dict) else None
    hit = value.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = alias
    miss = value.get("prompt_cache_miss_tokens")
    if alias is not None and (type(alias) is not int or alias != hit):
        return None
    try:
        result = CacheUsage(hit, miss)
        result.check_total(usage[0])
    except ValueError:
        return None
    return result
