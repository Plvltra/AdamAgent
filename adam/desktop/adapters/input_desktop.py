"""只读判断调用线程桌面是否仍接收用户输入，不切换桌面或尝试解锁。"""

import ctypes
import sys
from ctypes import wintypes

from adam.contracts.errors import ToolError


def require_input_desktop() -> None:
    """UOI_IO 为假或查询失败时拒绝；锁屏/安全桌面不降级成普通桌面。

    GetThreadDesktop 返回借用句柄，不由本函数关闭。UOI_IO 仅说明当前桌面输入资格，
    不能证明目标窗口无遮挡或下一瞬间仍有焦点。
    """
    require_unlocked_session()
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentThreadId.restype = wintypes.DWORD
    user.GetThreadDesktop.argtypes = [wintypes.DWORD]
    user.GetThreadDesktop.restype = wintypes.HANDLE
    user.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user.GetUserObjectInformationW.restype = wintypes.BOOL
    desktop = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    receiving, needed = wintypes.BOOL(), wintypes.DWORD()
    if (
        not desktop
        or not user.GetUserObjectInformationW(
            desktop, 6, ctypes.byref(receiving), ctypes.sizeof(receiving), ctypes.byref(needed)
        )
        or not receiving.value
    ):
        raise ToolError(
            "Input desktop is unavailable, locked or replaced; observe again after restoring it"
        )


class _SessionPrefix(ctypes.Structure):
    _fields_ = [("session_id", wintypes.DWORD), ("state", ctypes.c_int), ("flags", wintypes.LONG)]


class _LevelPrefix(ctypes.Union):
    # WTSINFOEX_LEVEL1 含 LARGE_INTEGER，联合按8字节对齐；只读取公开状态前缀。
    _fields_ = [("session", _SessionPrefix), ("alignment", ctypes.c_ulonglong)]


class _InfoPrefix(ctypes.Structure):
    _fields_ = [("level", wintypes.DWORD), ("data", _LevelPrefix)]


def require_unlocked_session() -> None:
    """WTSInfoEx 必须明确为活动且解锁；LockApp 可能仍让 UOI_IO 为真。

    首版限 Windows10+，不猜测旧系统反向锁标志；未知/查询失败均拒绝。只读取状态前缀，
    不记录原生缓冲区中的用户名和其他会话信息，始终释放 WTS 分配的内存。
    """
    if sys.getwindowsversion().major < 10:
        raise ToolError("Desktop actions require Windows 10 or later")
    api = ctypes.WinDLL("wtsapi32", use_last_error=True)
    api.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.WTSQuerySessionInformationW.restype = wintypes.BOOL
    api.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    pointer, length = ctypes.c_void_p(), wintypes.DWORD()
    if not api.WTSQuerySessionInformationW(
        None, 0xFFFFFFFF, 25, ctypes.byref(pointer), ctypes.byref(length)
    ):
        raise ToolError("Unable to verify desktop session lock state")
    try:
        if not pointer or length.value < ctypes.sizeof(_InfoPrefix):
            raise ToolError("Invalid desktop session status")
        info = ctypes.cast(pointer, ctypes.POINTER(_InfoPrefix)).contents
        if info.level != 1 or info.data.session.state != 0 or info.data.session.flags != 1:
            raise ToolError(
                "Desktop session is locked, disconnected or unknown; unlock then observe again"
            )
    finally:
        api.WTSFreeMemory(pointer)
