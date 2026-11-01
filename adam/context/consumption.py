"""从本次实际发送的消息建立单位计划；不把媒体字节或时长猜成 token。"""

from typing import Any

from adam.config.consumption import ConsumptionConfig
from adam.contracts.consumption import ConsumptionPlan, Quantities
from adam.contracts.errors import BudgetError, FailureCode, LLMError, RequestNotSentError
from adam.contracts.model_input import message_media


def plan_consumption(
    config: ConsumptionConfig,
    model: str,
    messages: list[dict[str, Any]],
    input_tokens: int,
    output_tokens: int,
    *,
    purpose: str = "completion",
) -> ConsumptionPlan | None:
    """计本次提交的图片份数及一次适配器尝试；关闭时不改变旧请求记录。"""
    if not config.enabled:
        return None
    images = 0
    for message in messages:
        for reference in message_media(message):
            if reference.mime_type.startswith("image/"):
                images += 1
            else:
                # 尚无可信音频时长端口，不能用零预留接受未来的音频输入。
                raise BudgetError(
                    "输入媒体缺少可信的单位计量，未发送请求。", code=FailureCode.USAGE_UNKNOWN
                )
    return ConsumptionPlan(
        Quantities(input_tokens, output_tokens, images, 0, 1), purpose, config.price(model)
    )


def settle_consumption(
    plan: ConsumptionPlan | None,
    usage: tuple[int, int] | None,
    error: BaseException | None = None,
) -> dict[str, Any] | None:
    """连接未建立或本地输入拒绝为零提交；不明失败保留媒体未知，调用数只证明尝试。"""
    if plan is None:
        return None
    not_sent = isinstance(error, RequestNotSentError) or (
        isinstance(error, LLMError)
        and error.code == FailureCode.MODEL_INPUT_INVALID
        and error.usage == (0, 0)
    )

    def submitted(count: int | None) -> int | None:
        if not_sent:
            return 0
        return count if error is None or usage is not None or count == 0 else None

    amounts = Quantities(
        usage[0] if usage is not None else None,
        usage[1] if usage is not None else None,
        submitted(plan.reservation.images),
        submitted(plan.reservation.audio_ms),
        1,
    )
    return plan.settlement(amounts)
