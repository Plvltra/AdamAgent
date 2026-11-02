"""固定ADB边界替身与真实媒体/租约；实际截图由独立视觉子代理验收。"""

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.mobile.observation import AndroidObserver
from tests.features.test_media_evidence import active_media, png


class Device(AndroidObserver):
    """仅替换ADB传输，不执行截图；返回已有PNG fixture及显式设备状态。"""

    def __init__(self, root):
        super().__init__(root / "adb.exe", "emulator-5556", lease_root=root)
        self.rotation, self.transport, self.online = 0, 1, True
        self.xml = b'<hierarchy rotation="0"><node text="Practice" /></hierarchy>'
        self.unstable = False

    def _run(self, args, cancel, **kwargs):
        cancel.check()
        if args == ["devices", "-l"]:
            return (
                f"emulator-5556 device product:test transport_id:{self.transport}\n".encode()
                if self.online
                else b"List of devices attached\n"
            )
        if args == ["shell", "cat", "/proc/sys/kernel/random/boot_id"]:
            return b"12345678-1234-1234-1234-123456789abc"
        if args == ["shell", "dumpsys", "window"]:
            return (
                (
                    "mCurrentFocus=Window{123 u0 com.adam.practice/.MainActivity}\\n"
                    f'mDisplayInfo=DisplayInfo{{"Built-in Screen", displayId 0, rotation {self.rotation}, state ON}}'
                )
                .replace("\\n", "\n")
                .encode()
            )
        if args == ["shell", "dumpsys", "input"]:
            return f"SurfaceOrientation: {self.rotation}".encode()
        if args[:2] == ["shell", "uiautomator"]:
            return b"dumped"
        if args[:2] == ["exec-out", "cat"]:
            return self.xml
        if args == ["exec-out", "screencap", "-p"]:
            if self.unstable:
                self.rotation = 1
            return png()
        raise AssertionError(args)


def test_observation_publishes_original_media_and_tracks_page_version(tmp_path):
    with active_media(tmp_path / "session") as (store, source, media):
        observer = Device(tmp_path)
        first = observer.observe(source, media.publish, CancellationToken())
        assert first.image.source == source
        assert media.read(first.image.media_id, expected_version=first.image.version).data == png()
        with observer.lease():
            assert observer.require_current(first.frame.version, CancellationToken()) == first.frame
        observer.xml = b'<hierarchy rotation="0"><node text="Changed" /></hierarchy>'
        with pytest.raises(RuntimeError, match="changed"):
            observer.require_current(first.frame.version, CancellationToken())
        second = observer.observe(source, media.publish, CancellationToken())
        assert second.frame.version != first.frame.version
        assert (
            len(
                [
                    event
                    for event in store.load_events(source.session_id)
                    if event["type"] == "media_saved"
                ]
            )
            == 2
        )


@pytest.mark.parametrize("change", ["rotation", "disconnect", "reconnect"])
def test_device_changes_invalidate_old_observation_until_observed_again(tmp_path, change):
    with active_media(tmp_path / "session") as (_, source, media):
        observer = Device(tmp_path)
        first = observer.observe(source, media.publish, CancellationToken())
        if change == "rotation":
            observer.rotation = 1
        elif change == "disconnect":
            observer.online = False
        else:
            observer.transport = 2
        with pytest.raises(RuntimeError):
            observer.require_current(first.frame.version, CancellationToken())
        observer.online = True
        with pytest.raises(ValueError, match="stale"):
            observer.require_current(first.frame.version, CancellationToken())
        observer.observe(source, media.publish, CancellationToken())


@pytest.mark.parametrize("cancelled", [False, True])
def test_unstable_or_cancelled_capture_never_publishes_media(tmp_path, cancelled):
    with active_media(tmp_path / "session") as (store, source, media):
        observer, cancel = Device(tmp_path), CancellationToken()
        if cancelled:
            cancel.cancel()
        else:
            observer.unstable = True
        with pytest.raises((RuntimeError, RequestInterrupted)):
            observer.observe(source, media.publish, cancel)
        assert not any(
            event["type"] == "media_saved" for event in store.load_events(source.session_id)
        )


def test_same_device_lease_is_exclusive_and_released_on_failure(tmp_path):
    one, two = Device(tmp_path), Device(tmp_path)
    with pytest.raises(ValueError):
        with one.lease():
            with pytest.raises(RuntimeError, match="leased"):
                with two.lease():
                    pytest.fail("second owner acquired device")
            raise ValueError("injected operation failure")
    with two.lease():
        pass
