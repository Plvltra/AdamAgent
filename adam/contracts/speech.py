"""已由用户确认的语音输入；正文仍是普通用户文本，录音仅作为来源证据。"""

import re
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class ConfirmedSpeech:
    """宿主从固定草稿取出的确认文字与原WAV；不能靠转写内容增加工具权限。"""

    capture_id: str
    text: str
    audio: bytes
    version: str
    device: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", self.capture_id) is None:
            raise ValueError("invalid speech capture identity")
        if not self.text.strip() or len(self.text) > 8000:
            raise ValueError("confirmed speech text is empty or too long")
        if not 0 < len(self.audio) <= 16777216 or sha256(self.audio).hexdigest() != self.version:
            raise ValueError("confirmed speech audio changed")
