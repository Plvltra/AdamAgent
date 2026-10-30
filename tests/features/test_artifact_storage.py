"""T8.1 附件存储业务：真实文件、会话配额和重启读取，尚不接入模型工具循环。"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
from filelock import FileLock

import adam.session.artifacts as artifact_module
from adam.contracts.artifacts import ArtifactSource
from adam.contracts.errors import StorageError
from adam.session.artifacts import ArtifactStore


def source(session_id="a" * 8):
    return ArtifactSource(session_id, "b" * 32, "c" * 32, "call-1", "run_command")


def test_long_unicode_text_is_paged_exactly_after_reopening(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts", max_read_chars=777)
    text = "\ufeff汉🙂line\r\n\0末行" * 1000
    reference = store.save(source(), text)
    assert reference.complete and reference.total_bytes == len(text.encode("utf-8"))
    assert reference.version == hashlib.sha256(text.encode("utf-8")).hexdigest()
    reopened = ArtifactStore(store.root, max_read_chars=777)
    assert reopened.describe(source().session_id, reference.artifact_id) == reference
    offset, pages = 0, []
    while True:
        page = reopened.read(
            source().session_id,
            reference.artifact_id,
            expected_version=reference.version,
            offset=offset,
        )
        assert page.reference == reference and page.offset == offset
        assert len(page.text) <= 777
        pages.append(page.text)
        if page.next_offset is None:
            break
        assert page.next_offset == offset + len(page.text)
        offset = page.next_offset
    assert len(pages) > 1 and "".join(pages) == text
    end = reopened.read(
        source().session_id,
        reference.artifact_id,
        expected_version=reference.version,
        offset=len(text),
    )
    assert end.text == "" and end.next_offset is None


def test_shared_byte_quota_saves_only_complete_utf8_characters(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=8, max_session_bytes=10)
    first = store.save(source(), "abcdef")
    second = store.save(source(), "中文XYZ")
    assert first.complete and first.saved_bytes == 6
    assert not second.complete and second.saved_bytes == 3 and second.total_bytes == 9
    page = store.read(source().session_id, second.artifact_id, expected_version=second.version)
    assert page.text == "中" and page.next_offset is None
    assert not page.reference.complete  # 读完已保存部分不能变成取得了完整原输出。
    last = store.save(source(), "Z")
    assert last.complete and last.saved_bytes == 1
    with pytest.raises(StorageError, match="字节配额"):
        store.save(source(), "one more")
    other = store.save(source("d" * 8), "abcdefgh")
    assert other.complete and other.saved_bytes == 8


def test_per_artifact_limit_and_upstream_incomplete_output_are_distinct(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=5)
    limited = store.save(source(), "ab🙂z")
    assert limited.saved_bytes == 2 and limited.total_bytes == 7 and not limited.complete
    unknown = store.save(source(), "abc", source_complete=False)
    assert unknown.saved_bytes == 3 and unknown.total_bytes is None and not unknown.complete
    assert store.describe(source().session_id, unknown.artifact_id) == unknown
    with pytest.raises(StorageError, match="完整字符"):
        ArtifactStore(tmp_path / "tiny", max_bytes=2).save(source(), "中")


def test_empty_text_is_valid_but_file_count_stays_bounded(tmp_path):
    store = ArtifactStore(tmp_path, max_items=2)
    empty = store.save(source(), "")
    assert empty.complete and empty.saved_bytes == empty.saved_chars == empty.total_bytes == 0
    store.save(source(), "one")
    with pytest.raises(StorageError, match="数量"):
        store.save(source(), "")
    assert len(list((tmp_path / source().session_id).glob("*.json"))) == 2


def test_existing_id_cannot_be_replaced_even_by_same_content(tmp_path):
    store = ArtifactStore(tmp_path)
    item = store.save(source(), "fixed", artifact_id="e" * 32)
    path = tmp_path / source().session_id / f"{item.artifact_id}.json"
    original = path.read_bytes()
    for value in ("fixed", "changed"):
        with pytest.raises(StorageError, match="拒绝覆盖"):
            store.save(source(), value, artifact_id=item.artifact_id)
        assert path.read_bytes() == original
    with pytest.raises(StorageError, match="版本"):
        store.read(source().session_id, item.artifact_id, expected_version="f" * 64)
    assert store.describe(source().session_id, item.artifact_id) == item


def test_external_replacement_cannot_reuse_an_old_content_reference(tmp_path):
    """外部写入即使提供自洽的新内容和哈希，旧引用也不能悄悄读取新版本。"""
    store = ArtifactStore(tmp_path)
    old = store.save(source(), "original")
    fresh = store.save(source(), "replaced")
    path = tmp_path / source().session_id / f"{old.artifact_id}.json"
    new_path = path.with_name(f"{fresh.artifact_id}.json")
    payload = json.loads(new_path.read_bytes())
    payload["reference"]["artifact_id"] = old.artifact_id
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StorageError, match="版本"):
        store.read(source().session_id, old.artifact_id, expected_version=old.version)


def test_other_session_cannot_read_or_relabel_an_artifact(tmp_path):
    store = ArtifactStore(tmp_path)
    item = store.save(source(), "session A only")
    other = "d" * 8
    with pytest.raises(StorageError):
        store.read(other, item.artifact_id, expected_version=item.version)
    original = tmp_path / source().session_id / f"{item.artifact_id}.json"
    copied = tmp_path / other / original.name
    copied.parent.mkdir()
    copied.write_bytes(original.read_bytes())
    with pytest.raises(StorageError, match="来源"):
        store.describe(other, item.artifact_id)
    assert store.describe(source().session_id, item.artifact_id) == item


def test_corrupt_existing_artifact_cannot_be_ignored_to_free_quota(tmp_path):
    store = ArtifactStore(tmp_path, max_session_bytes=8)
    item = store.save(source(), "1234567")
    path = tmp_path / source().session_id / f"{item.artifact_id}.json"
    path.write_bytes(b'{"format_version":1,')
    with pytest.raises(StorageError, match="损坏"):
        store.save(source(), "new")
    assert list(path.parent.glob("*.json")) == [path]
    assert path.read_bytes() == b'{"format_version":1,'


@pytest.mark.parametrize("boundary", ["write", "fsync", "publish"])
def test_failed_publish_returns_no_reference_and_preserves_existing_data(
    tmp_path, monkeypatch, boundary
):
    """在真实临时写入/同步/发布边界注入 IO 故障，不模拟业务函数的成功返回。"""
    store = ArtifactStore(tmp_path)
    first = store.save(source(), "keep")
    path = tmp_path / source().session_id / f"{first.artifact_id}.json"
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected private storage detail")

    class FailedWriter:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def write(self, data):
            fail()

    with monkeypatch.context() as patch:
        if boundary == "write":
            create = artifact_module.tempfile.NamedTemporaryFile
            patch.setattr(
                artifact_module.tempfile,
                "NamedTemporaryFile",
                lambda *args, **kwargs: FailedWriter(create(*args, **kwargs)),
            )
        else:
            patch.setattr(artifact_module.os, "fsync" if boundary == "fsync" else "replace", fail)
        with pytest.raises(StorageError) as caught:
            store.save(source(), "never published")
        assert "private storage detail" not in str(caught.value)
    assert path.read_bytes() == original
    assert list(path.parent.glob("*.json")) == [path]
    assert not list(path.parent.glob(".artifact-*.tmp"))
    assert store.describe(source().session_id, first.artifact_id) == first


def test_session_lock_and_concurrent_saves_cannot_overrun_quota(tmp_path):
    store = ArtifactStore(tmp_path, max_session_bytes=10)
    store.save(source("d" * 8), "separate")
    lock = tmp_path / ".locks" / f"{source().session_id}.lock"
    with FileLock(lock, timeout=0):
        with pytest.raises(StorageError, match="正在访问"):
            store.save(source(), "blocked")
    barrier = Barrier(2)

    def save(index):
        client = ArtifactStore(tmp_path, max_session_bytes=10)
        barrier.wait(timeout=5)
        try:
            return client.save(replace(source(), request_id=f"{index:032x}"), "abcdefgh")
        except StorageError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = [value for value in pool.map(save, [1, 2]) if value is not None]
    assert 1 <= len(saved) <= 2 and sum(value.saved_bytes for value in saved) <= 10
    assert all(store.describe(source().session_id, item.artifact_id) == item for item in saved)
    assert len(list((tmp_path / source().session_id).glob("*.json"))) == len(saved)


def test_unpublished_temporary_is_not_loaded_or_promoted_after_restart(tmp_path):
    store = ArtifactStore(tmp_path)
    first = store.save(source(), "published")
    directory = tmp_path / source().session_id
    pending = directory / ".artifact-orphan.tmp"
    pending.write_bytes(b'{"text":"unfinished"}')
    reopened = ArtifactStore(tmp_path)
    assert reopened.describe(source().session_id, first.artifact_id) == first
    with pytest.raises(StorageError):
        reopened.describe(source().session_id, "e" * 32)
    assert pending.read_bytes() == b'{"text":"unfinished"}'
    assert list(directory.glob("*.json")) == [directory / f"{first.artifact_id}.json"]


def test_resolved_redirect_to_another_session_is_rejected_before_open(tmp_path, monkeypatch):
    """模拟 Path.resolve 返回链接重定向；不依赖 Windows 创建符号链接的权限。"""
    store = ArtifactStore(tmp_path / "artifacts")
    item = store.save(source(), "private")
    candidate = store.root / source().session_id / f"{item.artifact_id}.json"
    resolve, original_open = Path.resolve, Path.open
    opened = []

    def redirected(path, *args, **kwargs):
        if path == candidate:
            return store.root / ("d" * 8) / candidate.name
        return resolve(path, *args, **kwargs)

    def observe(path, *args, **kwargs):
        opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    monkeypatch.setattr(Path, "open", observe)
    with pytest.raises(StorageError, match="重定向"):
        store.describe(source().session_id, item.artifact_id)
    assert opened == []
