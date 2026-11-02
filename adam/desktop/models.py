"""桌面观察使用物理屏幕像素，图像和控件共同绑定目标身份。"""

from dataclasses import dataclass
from typing import Protocol

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef


@dataclass(frozen=True)
class WindowIdentity:
    """窗口句柄、进程和 UIA 运行时身份；标题不是授权。"""

    handle: int
    process_id: int
    runtime_id: tuple[int, ...]


@dataclass(frozen=True)
class Control:
    """rect 为物理屏幕坐标，parent 为本次树中父节点索引。"""

    parent: int | None
    runtime_id: tuple[int, ...]
    name: str
    control_type: str
    automation_id: str
    rect: tuple[int, int, int, int]
    enabled: bool


@dataclass(frozen=True)
class DesktopFrame:
    """同次观察的树与 PNG；适配器检查前后身份、位置和焦点。"""

    identity: WindowIdentity
    title: str
    rect: tuple[int, int, int, int]
    dpi: int
    width: int
    height: int
    controls: tuple[Control, ...]
    png: bytes


@dataclass(frozen=True)
class Observation:
    """版本绑定完整观察，媒体与控件树共享宿主来源。"""

    version: str
    frame: DesktopFrame
    image: MediaRef

    def screen_point(self, x: float, y: float) -> tuple[int, int]:
        """截图像素映射到物理屏幕；拒绝越界，不把 DPI 再乘一次。"""
        if not 0 <= x < self.frame.width or not 0 <= y < self.frame.height:
            raise ValueError("Point outside observed image")
        left, top, right, bottom = self.frame.rect
        return (
            left + int(x * (right - left) / self.frame.width),
            top + int(y * (bottom - top) / self.frame.height),
        )


class DesktopBackend(Protocol):
    def observe(self, cancel: CancellationToken) -> DesktopFrame:
        """只观察宿主绑定目标，不自动激活或切换窗口。"""
        ...
