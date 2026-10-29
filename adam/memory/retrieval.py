"""不调用模型的关键词排序；只处理调用方已完成范围过滤的记录。"""

import re
from datetime import datetime

from adam.contracts.memory import MemoryRecord


def keywords(text: str) -> set[str]:
    """英文按词、中文按相邻双字匹配，避免中文无空格时整句只能精确命中。"""
    result = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(segment[index : index + 2] for index in range(len(segment) - 1))
    return result


def rank_records(records: list[MemoryRecord], query: str, now: datetime) -> list[MemoryRecord]:
    """按重合词数排序，只返回有效且命中的记录；同分时保持确定的更新时间与 ID 顺序。"""
    terms = keywords(query)
    scored = [
        (len(terms & keywords(record.content)), record) for record in records if record.active(now)
    ]
    scored.sort(key=lambda item: (item[0], item[1].updated_at, item[1].memory_id), reverse=True)
    return [record for score, record in scored if score > 0]
