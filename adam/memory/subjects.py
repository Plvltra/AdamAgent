"""先用显式主体和显示名消歧，再把有效范围交给相关性排序；不推断人物身份。"""

import re

from adam.contracts.memory import MemoryRecord


def select_subjects(records: list[MemoryRecord], query: str) -> tuple[list[MemoryRecord], str]:
    """@主体ID 优先；同名多个 ID 时排除这些含糊条目并提示，不按相似度替用户猜人。

    无主体指向时保留带身份标签的记录，普通资料仍可召回；调用方先过滤范围与时间。
    返回待排序的原版本及有界警告，不修改记忆或记录新的身份关系。
    """
    known = {record.subject.key for record in records if record.subject is not None}
    explicit = set(re.findall(r"(?<![\w@])@([A-Za-z0-9._:-]+)", query))
    if explicit:
        if not explicit <= known:
            return [], "指定主体没有当前有效记忆，请用 /memory list 核对主体 ID。"
        return [
            record for record in records if record.subject and record.subject.key in explicit
        ], ""
    names: dict[str, set[str]] = {}
    for record in records:
        if record.subject and record.subject.name in query:
            names.setdefault(record.subject.name, set()).add(record.subject.key)
    ambiguous = {name: keys for name, keys in names.items() if len(keys) > 1}
    selected = set().union(*(keys for name, keys in names.items() if name not in ambiguous))
    rejected = set().union(*ambiguous.values())
    filtered = [
        record
        for record in records
        if record.subject is None
        or (record.subject.key not in rejected and (not names or record.subject.key in selected))
    ]
    warning = ""
    if ambiguous:
        choices = "; ".join(
            f"{name}: " + ", ".join("@" + key for key in sorted(keys)[:4])
            for name, keys in sorted(ambiguous.items())[:4]
        )
        warning = "记忆中存在同名主体，本轮未使用这些条目；请明确主体 ID：" + choices[:450]
        if not selected:
            # 只有含糊人物时，不用无主体的通用偏好冒充这个人的事实。
            filtered = []
    return filtered, warning
