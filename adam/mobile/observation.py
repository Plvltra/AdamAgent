"""固定模拟器的ADB观察；设备/前台/旋转变化使旧观察失效。"""

import hashlib
import re
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

from adam.contracts.cancellation import CancellationToken
from adam.contracts.media import MediaRef, MediaSource


@dataclass(frozen=True)
class MobileFrame:
    """完整UI XML与PNG共用一次设备状态；version绑定状态/页面/字节，不表示永远有效。"""

    serial: str
    device_version: str
    rotation: int
    width: int
    height: int
    hierarchy: str
    png: bytes
    version: str


@dataclass(frozen=True)
class MobileObservation:
    """原媒体边界发布的截图引用及同次完整观察。"""

    frame: MobileFrame
    image: MediaRef


class AndroidObserver:
    """仅允许显式本地模拟器及测试包；不发现或选择真实手机，不提供任意shell接口。"""

    def __init__(
        self,
        adb: Path,
        serial: str,
        *,
        port: int = 5041,
        package: str = "com.adam.practice",
        lease_root: Path | None = None,
    ) -> None:
        if re.fullmatch(r"emulator-[0-9]{4,5}", serial) is None or not 1024 <= port <= 65535:
            raise ValueError("explicit emulator serial and isolated adb port required")
        if package != "com.adam.practice":
            raise ValueError("only the isolated practice application is supported")
        self.adb, self.serial, self.port, self.package = adb.resolve(), serial, port, package
        self.lease_root = lease_root or Path(tempfile.gettempdir())
        self._last: MobileFrame | None = None

    @contextmanager
    def lease(self) -> Iterator[None]:
        """同serial跨宿主互斥；竞争立即拒绝，不排队使用过期页面，异常释放。"""
        self.lease_root.mkdir(parents=True, exist_ok=True)
        path = self.lease_root / ("adam-mobile-" + self.serial + ".lock")
        lock = FileLock(path)
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            self._last = None
            raise RuntimeError("device input is leased; observe again") from error
        try:
            yield
        finally:
            lock.release()

    def observe(
        self,
        source: MediaSource,
        publish: Callable[[MediaSource, bytes, str], MediaRef],
        cancel: CancellationToken,
    ) -> MobileObservation:
        """租约内两次核对设备和UI；不稳定/断连/取消不发布，成功沿原来源保存PNG。"""
        with self.lease():
            self._last = None
            before = self._state(cancel)
            xml = self._hierarchy(cancel)
            png = self._run(["exec-out", "screencap", "-p"], cancel, limit=16777216)
            after = self._state(cancel)
            if before != after or xml != self._hierarchy(cancel):
                raise RuntimeError("device or page changed during observation; observe again")
            if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("invalid screenshot PNG")
            width, height = struct.unpack(">II", png[16:24])
            if not 0 < width <= 8192 or not 0 < height <= 8192:
                raise ValueError("screenshot dimensions exceed limit")
            version = hashlib.sha256(before.encode() + xml.encode() + png).hexdigest()
            frame = MobileFrame(
                self.serial, before, int(before.split(":")[-1]), width, height, xml, png, version
            )
            cancel.check()
            image = publish(source, png, "image/png")
            self._last = frame
            return MobileObservation(frame, image)

    def require_current(self, version: str, cancel: CancellationToken) -> MobileFrame:
        """动作持有同设备租约时调用；只认当前宿主观察，失败后必须重新观察。

        比较连接身份、启动身份、前台、旋转和完整UI；状态栏时钟像素不用于页面比较。
        """
        frame = self._last
        try:
            if frame is None or frame.version != version:
                raise ValueError("unknown or stale observation")
            if (
                self._state(cancel) != frame.device_version
                or self._hierarchy(cancel) != frame.hierarchy
            ):
                raise RuntimeError("device or page changed; observe again")
            return frame
        except BaseException:
            self._last = None
            raise

    def _state(self, cancel: CancellationToken) -> str:
        """连接transport与boot身份区别重连/重启；要求测试包前台且设备在线。"""
        devices = self._run(["devices", "-l"], cancel, global_command=True).decode("utf-8")
        line = next(
            (
                line
                for line in devices.splitlines()
                if line.startswith(self.serial + " ") or line.startswith(self.serial + "\t")
            ),
            "",
        )
        transport = re.search(r"\btransport_id:(\d+)\b", line)
        if not re.match(re.escape(self.serial) + r"\s+device\s", line) or not transport:
            raise RuntimeError("emulator disconnected or unauthorized")
        boot = (
            self._run(["shell", "cat", "/proc/sys/kernel/random/boot_id"], cancel).decode().strip()
        )
        windows = self._run(["shell", "dumpsys", "window"], cancel).decode("utf-8")
        focus = re.search(r"mCurrentFocus=([^\n]+)", windows)
        if not focus or self.package + "/" not in focus.group(1):
            raise RuntimeError("practice application is not foreground")
        # API30的windows子命令不含前台；完整window的主屏DisplayInfo才是有效旋转。
        rotation = re.search(
            r"mDisplayInfo=DisplayInfo\{[^\n]*displayId 0,[^\n]* rotation ([0-3]),", windows
        )
        if not rotation or re.fullmatch(r"[0-9a-f-]{36}", boot) is None:
            raise RuntimeError("device identity or rotation is unavailable")
        return (
            self.serial
            + ":"
            + transport.group(1)
            + ":"
            + boot
            + ":"
            + focus.group(1).strip()
            + ":"
            + rotation.group(1)
        )

    def _hierarchy(self, cancel: CancellationToken) -> str:
        """固定临时层级文件；解析前限长，不向模型传任意ADB执行结果。"""
        self._run(["shell", "uiautomator", "dump", "/sdcard/adam-observation.xml"], cancel)
        raw = self._run(["exec-out", "cat", "/sdcard/adam-observation.xml"], cancel)
        root = ET.fromstring(raw)
        if root.tag != "hierarchy" or len(list(root.iter())) > 512:
            raise ValueError("invalid or oversized UI hierarchy")
        return ET.tostring(root, encoding="unicode")

    def _run(
        self,
        args: list[str],
        cancel: CancellationToken,
        *,
        limit: int = 2097152,
        global_command: bool = False,
    ) -> bytes:
        """固定参数数组及15秒期限；取消杀死自己的adb客户端，不停止共享服务。"""
        cancel.check()
        command = [str(self.adb), "-P", str(self.port)]
        if not global_command:
            command += ["-s", self.serial]
        command += args
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                command,
                stdout=out,
                stderr=err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                for _ in range(300):
                    cancel.check()
                    if process.poll() is not None:
                        break
                    if out.tell() > limit:
                        raise ValueError("adb output exceeds limit")
                    cancel.wait(0.05)
                else:
                    raise TimeoutError("adb command timed out")
                if process.returncode:
                    err.seek(0)
                    raise RuntimeError("adb failed: " + err.read(2000).decode("utf-8", "replace"))
                out.seek(0)
                raw = out.read(limit + 1)
                if len(raw) > limit:
                    raise ValueError("adb output exceeds limit")
                return raw
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
