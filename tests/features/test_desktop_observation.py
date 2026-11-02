"""桌面观察的媒体来源、版本与坐标边界；真实截图由独立视觉验收另测。"""

from dataclasses import replace

import pytest

from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.desktop.models import DesktopFrame, WindowIdentity
from adam.desktop.service import DesktopObserver
from tests.features.test_media_evidence import active_media, png


class Backend:
    def __init__(self):
        self.frame = DesktopFrame(
            WindowIdentity(123, 42, (1, 2)), "test", (100, 200, 400, 500), 144, 2, 2, (), png()
        )

    def observe(self, cancel):
        cancel.check()
        return self.frame


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_observation_versions_geometry_and_binds_real_media_source(tmp_path, dpi):
    backend = Backend()
    backend.frame = replace(backend.frame, dpi=dpi)
    with active_media(tmp_path) as (store, source, media):
        observer = DesktopObserver(backend, media.publish)
        first = observer.observe(source, CancellationToken())
        assert first.image.source == source
        assert media.read(first.image.media_id, expected_version=first.image.version).data == png()
        assert first.screen_point(1, 1) == (250, 350)
        backend.frame = replace(backend.frame, rect=(-100, 0, 500, 600))
        second = observer.observe(source, CancellationToken())
        assert second.version != first.version
        assert second.screen_point(1, 1) == (200, 300)
        with pytest.raises(ValueError):
            second.screen_point(2, 0)
        assert (
            len([e for e in store.load_events(source.session_id) if e["type"] == "media_saved"])
            == 2
        )


def test_cancelled_observation_has_no_media_side_effect(tmp_path):
    with active_media(tmp_path) as (store, source, media):
        token = CancellationToken()
        token.cancel()
        with pytest.raises(RequestInterrupted):
            DesktopObserver(Backend(), media.publish).observe(source, token)
        assert not any(e["type"] == "media_saved" for e in store.load_events(source.session_id))
