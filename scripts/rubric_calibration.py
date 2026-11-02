"""B33小金标校准及主观判据顺序交换；不把评判模型输出当真实成功。"""

from collections.abc import Callable, Mapping
from typing import Any, Literal

Verdict = Literal["left", "right", "tie", "abstain"]


def calibrate(gold: Mapping[str, bool], predictions: Mapping[str, str]) -> dict[str, Any]:
    """按固定样本ID统计通过误报/漏报；缺项算拒评，未知ID拒绝，拒评不算正确。

    金标须来自独立判据或人工标注，不应复制待校准评估器的输出。
    """
    if not gold or any(type(v) is not bool for v in gold.values()):
        raise ValueError("gold requires nonempty boolean labels")
    if predictions.keys() - gold.keys() or any(
        value not in {"pass", "fail", "abstain"} for value in predictions.values()
    ):
        raise ValueError("unknown sample or prediction")
    counts = dict(true_positive=0, true_negative=0, false_positive=0, false_negative=0, abstain=0)
    rows = []
    for key, expected in gold.items():
        predicted = predictions.get(key, "abstain")
        if predicted == "abstain":
            category = "abstain"
        elif predicted == "pass":
            category = "true_positive" if expected else "false_positive"
        else:
            category = "false_negative" if expected else "true_negative"
        counts[category] += 1
        rows.append({"id": key, "expected": expected, "predicted": predicted, "category": category})
    positives = sum(gold.values())
    negatives = len(gold) - positives
    return {
        "counts": counts,
        "rows": rows,
        "total": len(gold),
        "coverage": (len(gold) - counts["abstain"]) / len(gold),
        "accuracy_all": (counts["true_positive"] + counts["true_negative"]) / len(gold),
        "false_positive_rate": counts["false_positive"] / negatives if negatives else None,
        "false_negative_rate": counts["false_negative"] / positives if positives else None,
    }


def compare_swapped(left: str, right: str, judge: Callable[[str, str], Verdict]) -> dict[str, Any]:
    """将同一对答案交换顺序评两次，映射回原身份；不一致或拒评则不宣布优胜者。

    judge由调用方显式注入；此函数不会自己连接模型，异常直接交给调用方。
    """
    forward = judge(left, right)
    reverse = judge(right, left)
    mapping = {"left": "right", "right": "left", "tie": "tie", "abstain": "abstain"}
    if forward not in mapping or reverse not in mapping:
        raise ValueError("invalid judge verdict")
    mapped = mapping[reverse]
    consistent = forward == mapped and forward != "abstain"
    return {
        "forward": forward,
        "reverse": reverse,
        "reverse_original_identity": mapped,
        "consistent": consistent,
        "preference": forward if consistent else "abstain",
        "scope": "subjective_preference_only",
    }
