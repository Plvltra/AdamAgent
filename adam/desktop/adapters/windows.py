"""Windows UIA 观察，仅连接显式句柄和进程，不枚举桌面或自动聚焦。"""

import ctypes
import io
from collections import deque
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.desktop.models import Control, DesktopFrame, WindowIdentity


class WindowsDesktop:
    """只读适配器；UIA 和捕获是顺序观察，不保证应用内容原子性。"""

    def __init__(self, handle: int, process_id: int, *, max_controls: int = 200) -> None:
        if handle <= 0 or process_id <= 0 or not 1 <= max_controls <= 1000:
            raise ValueError("Explicit identity and bounded controls required")
        self.handle, self.process_id, self.max_controls = handle, process_id, max_controls
        self.identity: WindowIdentity | None = None

    def observe(self, cancel: CancellationToken) -> DesktopFrame:
        """DPI 感知线程读取树和窗口截图；失焦、移动、超限时整次拒绝。

        仅截取目标矩形；调用方使用独立测试窗口。截图可能含覆盖层，不能据此证明
        未被遮挡；前后状态检查减少竞争，但不是 Windows 原子快照。
        """
        from pywinauto import Desktop

        user: Any = ctypes.WinDLL("user32", use_last_error=True)
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user.GetForegroundWindow.restype = ctypes.c_void_p
        user.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        user.GetDpiForWindow.restype = ctypes.c_uint
        previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise OSError("Unable to enter DPI context")
        try:
            cancel.check()
            window = Desktop(backend="uia").window(handle=self.handle).wrapper_object()
            identity, rect, dpi = self._state(window, user)
            if self.identity is not None and identity != self.identity:
                raise ValueError("Window identity changed; explicit rebind required")
            queue: Any = deque([(window, None)])
            controls = []
            while queue:
                cancel.check()
                if len(controls) >= self.max_controls:
                    raise ValueError("Control tree exceeds limit")
                current, parent = queue.popleft()
                info = current.element_info
                if info.process_id != self.process_id:
                    raise ValueError("Cross-process UIA child not authorized")
                bounds = current.rectangle()
                controls.append(
                    Control(
                        parent,
                        tuple(info.runtime_id),
                        str(info.name)[:1000],
                        str(info.control_type),
                        str(info.automation_id)[:200],
                        (bounds.left, bounds.top, bounds.right, bounds.bottom),
                        bool(info.enabled),
                    )
                )
                index = len(controls) - 1
                children = current.children()
                if len(controls) + len(queue) + len(children) > self.max_controls:
                    raise ValueError("Control tree exceeds limit")
                queue.extend((child, index) for child in children)
            cancel.check()
            with window.capture_as_image() as image, io.BytesIO() as stream:
                width, height = image.size
                if width * height > 16000000:
                    raise ValueError("Window exceeds pixel limit")
                image.save(stream, format="PNG")
                png = stream.getvalue()
            cancel.check()
            if self._state(window, user) != (identity, rect, dpi):
                raise ValueError("Window changed during observation")
            self.identity = identity
            return DesktopFrame(
                identity,
                str(window.window_text())[:1000],
                rect,
                dpi,
                width,
                height,
                tuple(controls),
                png,
            )
        finally:
            user.SetThreadDpiAwarenessContext(previous)

    def _state(
        self,
        window: Any,  # noqa: ANN401 - UIA 动态包装器
        user: Any,  # noqa: ANN401 - UIA 与 ctypes 的动态 SDK 边界
    ) -> tuple[WindowIdentity, tuple[int, int, int, int], int]:
        """失焦、进程替换、最小化或非法尺寸时拒绝旧目标。"""
        info = window.element_info
        if (
            info.process_id != self.process_id
            or user.GetForegroundWindow() != self.handle
            or not window.is_visible()
            or window.is_minimized()
        ):
            raise ValueError("Target is not authorized visible foreground window")
        bounds = window.rectangle()
        rect = (bounds.left, bounds.top, bounds.right, bounds.bottom)
        dpi = int(user.GetDpiForWindow(self.handle))
        if (
            dpi <= 0
            or bounds.width() <= 0
            or bounds.height() <= 0
            or bounds.width() * bounds.height() > 16000000
        ):
            raise ValueError("Invalid window geometry or DPI")
        return WindowIdentity(self.handle, self.process_id, tuple(info.runtime_id)), rect, dpi
