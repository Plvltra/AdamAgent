"""在本地文件失败与检测器之间传递同次读取的版本；不从 JSON 声明推断证据。"""

from typing import Any

from adam.contracts.errors import ToolError


class FileStateError(ToolError):
    """文件已读取但操作失败；version 仅在完整、有界读取取得原字节后设置。"""

    def __init__(self, message: str, *, version: str | None = None) -> None:
        super().__init__(message)
        self.version = version


class FileResult(dict[str, Any]):
    """保留工具的普通 JSON 字段，另携带宿主核实的版本；序列化不暴露内部元数据。"""

    def __init__(self, value: dict[str, Any], *, version: str | None) -> None:
        super().__init__(value)
        self.version = version
