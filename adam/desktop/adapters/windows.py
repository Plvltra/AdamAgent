"""Windows UIA 观察，仅连接显式句柄和进程，不枚举桌面或自动聚焦。"""

import ctypes
import io
from collections import deque
from typing import Any

from adam.contracts.cancellation import CancellationToken
from adam.contracts.desktop import DesktopActionUnknownError, DesktopApproval
from adam.desktop.models import Control, DesktopFrame, WindowIdentity


class WindowsDesktop:
    """显式窗口适配器；UIA 和捕获是顺序观察，不保证应用内容原子性。"""

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

    def perform(
        self, plan: DesktopApproval, expected: DesktopFrame, cancel: CancellationToken
    ) -> None:
        """优先 UIA Invoke/Value，缺 Invoke 时才用原控件点击；键盘仅固定导航键。

        原生调用一旦进入，不猜测其是否产生副作用；异常停止本轮而不自动重复。
        """
        from pywinauto import Desktop, keyboard, mouse, uia_defines

        user: Any = ctypes.WinDLL("user32", use_last_error=True)
        user.GetForegroundWindow.restype = ctypes.c_void_p
        user.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        user.GetDpiForWindow.restype = ctypes.c_uint
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise OSError("Unable to enter DPI context")
        try:
            window = Desktop(backend="uia").window(handle=self.handle).wrapper_object()
            cancel.check()
            if self._state(window, user) != (expected.identity, expected.rect, expected.dpi):
                raise ValueError("Window changed before dispatch")
            target: Any = None
            point = plan.point
            if plan.control is not None:
                wanted = expected.controls[plan.control]
                queue: Any = deque([window])
                visited = 0
                while queue and visited < self.max_controls:
                    current = queue.popleft()
                    visited += 1
                    if tuple(current.element_info.runtime_id) == wanted.runtime_id:
                        target = current
                        break
                    queue.extend(current.children())
                if target is None or not target.is_enabled():
                    raise ValueError("Observed control disappeared or disabled")
                bounds = target.rectangle()
                point = ((bounds.left + bounds.right) // 2, (bounds.top + bounds.bottom) // 2)
                if (bounds.left, bounds.top, bounds.right, bounds.bottom) != wanted.rect:
                    raise ValueError("Observed control moved")
            invoke = None
            if plan.operation == "click" and target is not None:
                try:
                    invoke = target.iface_invoke
                except uia_defines.NoPatternInterfaceError:
                    pass
            if plan.operation == "fill" and (
                target is None or not hasattr(target, "set_edit_text")
            ):
                raise ValueError("Control has no supported text setter")
            cancel.check()
            if self._state(window, user) != (expected.identity, expected.rect, expected.dpi):
                raise ValueError("Target changed before dispatch")
            try:
                if plan.operation == "fill":
                    assert target is not None
                    target.set_edit_text(plan.value)
                elif plan.operation == "click":
                    if invoke is not None:
                        invoke.Invoke()
                    else:
                        # 不调用会自行激活对话框的 click_input；只用刚复核的物理坐标。
                        assert point is not None
                        mouse.click(coords=point)
                else:
                    assert plan.value is not None
                    keyboard.send_keys(
                        {
                            "TAB": "{TAB}",
                            "SHIFT+TAB": "+{TAB}",
                            "ENTER": "{ENTER}",
                            "ESC": "{ESC}",
                            "CTRL+A": "^a",
                        }[plan.value]
                    )
            except BaseException as error:
                raise DesktopActionUnknownError(
                    "Native desktop action outcome unknown; do not retry"
                ) from error
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
