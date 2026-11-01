"""冻结媒体复制仍有配额、原子发布和只读完整性约束。"""

from dataclasses import replace

import pytest

from adam.contracts.errors import StorageError
from adam.contracts.media import MediaContent
from adam.recording.format import RecordingError
from adam.recording.media import MediaArchive
from adam.session.media import MediaStore
from tests.test_model_input import reference


def test_restore_preserves_exact_identity_timestamp_and_is_idempotent(tmp_path):
    content = MediaContent(reference(), b"bounded bytes")
    files = MediaStore(tmp_path / "archive")
    files.restore(content)
    path = (
        files.root / content.reference.source.session_id / (content.reference.media_id + ".media")
    )
    before = path.read_bytes(), path.stat().st_mtime_ns
    files.restore(content)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert files.read_frozen(content.reference) == content
    with pytest.raises(StorageError):
        files.restore(
            MediaContent(
                replace(content.reference, created_at="2026-11-01T01:00:00+00:00"), content.data
            )
        )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("limits", [{"max_bytes": 1}, {"max_session_bytes": 20}, {"max_items": 1}])
def test_restore_checks_full_size_and_cumulative_quota_without_truncation(tmp_path, limits):
    files = MediaStore(tmp_path, **limits)
    first = reference()
    if "max_bytes" in limits:
        with pytest.raises(StorageError):
            files.restore(MediaContent(first, b"bounded bytes"))
        assert not list(tmp_path.rglob("*.media"))
    else:
        files.restore(MediaContent(first, b"bounded bytes"))
        with pytest.raises(StorageError):
            files.restore(MediaContent(replace(first, media_id="e" * 32), b"bounded bytes"))
        assert len(list(tmp_path.rglob("*.media"))) == 1


def test_restore_verifies_bytes_and_preserves_old_file_on_atomic_publish_failure(
    tmp_path, monkeypatch
):
    files = MediaStore(tmp_path)
    content = MediaContent(reference(), b"bounded bytes")
    with pytest.raises(StorageError):
        files.restore(MediaContent(content.reference, b"corrupt"))
    files.restore(content)
    original = files.read_frozen(content.reference)

    def fail(*args, **kwargs):
        raise OSError("injected archive publish failure")

    monkeypatch.setattr(files, "_publish", fail)
    with pytest.raises(StorageError):
        files.restore(MediaContent(replace(content.reference, media_id="e" * 32), content.data))
    assert files.read_frozen(content.reference) == original
    assert len(list(tmp_path.rglob("*.media"))) == 1


def test_read_frozen_missing_never_creates_root_or_lock(tmp_path):
    files = MediaStore(tmp_path / "missing")
    with pytest.raises(StorageError):
        files.read_frozen(reference())
    assert not files.root.exists()


def test_read_frozen_refuses_redirected_owner_before_open(tmp_path, monkeypatch):
    files = MediaStore(tmp_path / "archive")
    original = type(files.root).resolve

    def redirect(path, *args, **kwargs):
        if path == files.root / reference().source.session_id / (reference().media_id + ".media"):
            return tmp_path / "another-owner.media"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(files.root), "resolve", redirect)
    with pytest.raises(StorageError, match="重定向"):
        files.read_frozen(reference())
    assert not files.root.exists()


@pytest.mark.parametrize("boundary", ["single", "bytes", "items"])
def test_archive_checks_total_read_limits_before_opening_any_binary(
    tmp_path, monkeypatch, boundary
):
    archive = MediaArchive(tmp_path / "capture.jsonl")
    if boundary == "single":
        refs = (replace(reference(), size_bytes=archive.files.max_bytes + 1),)
    elif boundary == "bytes":
        refs = tuple(
            replace(reference(), media_id=f"{i:032x}", size_bytes=archive.files.max_bytes)
            for i in range(7)
        )
    else:
        refs = tuple(
            replace(reference(), media_id=f"{i:032x}") for i in range(archive.files.max_items + 1)
        )

    def fail(*args, **kwargs):
        pytest.fail("oversized archive must be rejected before opening any binary")

    monkeypatch.setattr(archive.files, "read_frozen", fail)
    with pytest.raises(RecordingError, match="上限"):
        archive.verify(refs)
    assert not archive.files.root.exists()
