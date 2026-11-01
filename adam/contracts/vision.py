"""视觉问答的来源结果与未知费用证据；不把附图存在当作语义正确。"""

from dataclasses import asdict, dataclass
from typing import Any

from adam.contracts.media import MediaRef


@dataclass(frozen=True)
class ImageAnswer:
    """已保存的回答及确切输入图；images 是宿主来源绑定，不是模型定位准确性证明。"""

    text: str
    images: tuple[MediaRef, ...]
    session_id: str
    turn_id: str
    request_id: str


def image_question_evidence(reference: MediaRef) -> dict[str, Any]:
    """记录实际输入计划及无价格状态；不猜测本地算力成本或把图片折算成计费 token。"""
    return {
        "version": 1,
        "backend": "llama_cpp",
        "images": [asdict(reference)],
        "cost": None,
        "currency": None,
        "cost_status": "unpriced",
    }


def validate_image_question(value: object, media: object) -> None:
    """视觉请求证据只能绑定本轮一张完整原图；费用未知，不能伪造为已计价。"""
    if (
        not isinstance(value, dict)
        or value.keys() != {"version", "backend", "images", "cost", "currency", "cost_status"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or value["backend"] != "llama_cpp"
        or not isinstance(media, list)
        or len(media) != 1
        or value["images"] != media
        or value["cost"] is not None
        or value["currency"] is not None
        or value["cost_status"] != "unpriced"
    ):
        raise ValueError("invalid image question evidence")
