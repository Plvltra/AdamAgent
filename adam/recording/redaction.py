"""本地规则脱敏；查找候选后全局替换，报告不保存敏感原文或可逆映射。"""

import json
import math
import re
import secrets
from dataclasses import dataclass
from typing import Any

from adam.recording.format import RecordingError

RULES_VERSION = "local-redaction-v1"
CATEGORIES = frozenset({"email", "credential", "address", "identifier", "text"})
SECRET_KEYS = frozenset(
    {"password", "passwd", "secret", "apikey", "accesstoken", "refreshtoken", "authorization"}
)
ADDRESS_KEYS = frozenset({"address", "homeaddress", "postaladdress", "地址", "住址"})
PATTERNS = (
    ("email", re.compile(r"(?<![\w.+-])[\w.+-]{1,128}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}\b")),
    ("credential", re.compile(r"\bsk-[A-Za-z0-9_-]{16,200}\b")),
    ("credential", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/=-]{4,4096})")),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret)"
            r"[\"']?\s*[:=]\s*[\"']?([^\s\"',;<>}{\[\]\\]{1,4096})"
        ),
    ),
)
MAX_VALUES = 256
MAX_FINDINGS = 10000
MAX_NODES = 500000


@dataclass(frozen=True)
class LiteralRedaction:
    """宿主补充的精确文本规则，适用于难以自动识别的地址、姓名或任意秘密。"""

    value: str
    category: str = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not 1 <= len(self.value) <= 4096:
            raise RecordingError("脱敏字面规则必须包含 1～4096 个字符。")
        if not isinstance(self.category, str) or self.category not in CATEGORIES:
            raise RecordingError("脱敏规则类别不支持。")


def redact_json(
    value: object, literals: tuple[LiteralRedaction, ...] = ()
) -> tuple[object, list[dict[str, Any]]]:
    """返回脱敏副本和位置报告；不修改输入，不读取配置、请求模型或保证识别所有秘密。

    自动识别邮箱、常见凭据文本及敏感字段，地址可通过字段或字面规则指定。
    同一文本在本次导出中使用同一占位符；报告仅含序号位置、类别和占位符。
    任一深度、节点、规则或替换数量超限均失败，不返回部分脱敏结果。
    """
    redactor = _Redactor(literals)
    redactor.collect(value)
    return redactor.transform(value), redactor.findings


def _field_category(key: str) -> str | None:
    normalized = key.casefold().replace("_", "").replace("-", "")
    if normalized in SECRET_KEYS:
        return "credential"
    if normalized in ADDRESS_KEYS:
        return "address"
    return None


class _Redactor:
    """原文到占位符的映射仅留在本次调用内；先发现，后替换未标注的重复文本。"""

    def __init__(self, literals: tuple[LiteralRedaction, ...]) -> None:
        if len(literals) > 128 or any(not isinstance(r, LiteralRedaction) for r in literals):
            raise RecordingError("脱敏字面规则最多 128 条，且必须使用 LiteralRedaction。")
        self.values: dict[str, str] = {}
        self.tokens: dict[tuple[str, str], str] = {}
        self.findings: list[dict[str, Any]] = []
        self.prefix = "<ADAM_REDACTED_" + secrets.token_hex(8) + "_"
        self.nodes = 0
        self.matcher: re.Pattern[str] | None = None
        for rule in literals:
            self._remember(rule.value, rule.category)

    def collect(self, value: object, depth: int = 0) -> None:
        """发现已知敏感值；字段和值都扫描，不将用户字典键复制到位置报告。"""
        self._bounded(depth)
        if isinstance(value, str):
            if self.prefix in value:
                raise RecordingError("脱敏占位符与输入碰撞，请重新导出。")
            for category, pattern in PATTERNS:
                for match in pattern.finditer(value):
                    self._remember(match.group(1) if match.lastindex else match.group(), category)
        elif isinstance(value, list):
            for child in value:
                self.collect(child, depth + 1)
        elif isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise RecordingError("脱敏仅接受 JSON 字符串字典键。")
                category = _field_category(key)
                if category and isinstance(child, str) and child:
                    self._remember(child, category)
                self.collect(key, depth + 1)
                self.collect(child, depth + 1)
        elif value is not None and type(value) not in {bool, int, float}:
            raise RecordingError("脱敏仅接受 JSON 数据。")
        elif isinstance(value, float) and not math.isfinite(value):
            raise RecordingError("脱敏不接受非有限 JSON 数值。")

    def transform(self, value: object) -> object:
        """用固定文字匹配替换，不执行用户提供的正则；保留普通值与容器顺序。"""
        self.nodes = 0
        if self.values:
            self.matcher = re.compile(
                "|".join(re.escape(v) for v in sorted(self.values, key=len, reverse=True))
            )
        return self._walk(value, "$", 0)

    def _walk(self, value: object, location: str, depth: int) -> object:
        self._bounded(depth)
        if isinstance(value, str):
            return self._text(value, location)
        if isinstance(value, list):
            return [
                self._walk(child, f"{location}[{i}]", depth + 1) for i, child in enumerate(value)
            ]
        if isinstance(value, dict):
            result = {}
            for i, (key, child) in enumerate(value.items()):
                # 仅使用原容器序号定位；报告路径本身不能再泄漏敏感键。
                field = f"{location}.value[{i}]"
                name = self._text(key, f"{location}.key[{i}]")
                if name in result:
                    raise RecordingError("脱敏后出现重复字段，未导出。")
                category = _field_category(key)
                result[name] = (
                    self._replacement(child, category, field)
                    if category
                    else self._walk(child, field, depth + 1)
                )
            return result
        return value

    def _text(self, value: str, location: str) -> str:
        if self.matcher is None:
            return value

        def replace(match: re.Match[str]) -> str:
            original = match.group()
            return self._replacement(original, self.values[original], location)

        return self.matcher.sub(replace, value)

    def _replacement(self, value: object, category: str, location: str) -> str:
        if len(self.findings) >= MAX_FINDINGS:
            raise RecordingError("脱敏替换超过 10000 处，未导出部分结果。")
        key = (category, json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
        if key not in self.tokens:
            self.tokens[key] = f"{self.prefix}{category}_{len(self.tokens) + 1}>"
        token = self.tokens[key]
        self.findings.append({"location": location, "category": category, "replacement": token})
        return token

    def _remember(self, value: str, category: str) -> None:
        if len(value) > 4096 or (value not in self.values and len(self.values) >= MAX_VALUES):
            raise RecordingError("脱敏候选值超限，未导出；请缩小录制范围。")
        if value not in self.values or category == "credential":
            self.values[value] = category

    def _bounded(self, depth: int) -> None:
        self.nodes += 1
        if depth > 64 or self.nodes > MAX_NODES:
            raise RecordingError("脱敏数据深度或节点数量超限，未导出。")
