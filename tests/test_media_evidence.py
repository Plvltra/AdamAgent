"""C00.2 来源、版本、文件封装及日志边界；存储故障和真实媒体闭环见 features。"""

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from adam.contracts.errors import StorageError
from adam.contracts.media import MediaRef, MediaSource, decode_media_reference
from adam.session.media import MediaStore
from adam.session.media_evidence import SessionMedia
from tests.test_hook_log import started_tool


def source(**changes):
    fields = {"session_id": "a" * 8, "turn_id": "b" * 32, "kind": "input"}
    fields.update(changes)
    return MediaSource(**fields)


def reference(**changes):
    fields = {
        "media_id": "c" * 32,
        "source": source(),
        "mime_type": "image/png",
        "size_bytes": 3,
        "version": hashlib.sha256(b"abc").hexdigest(),
        "created_at": "2026-11-01T00:00:00+00:00",
    }
    fields.update(changes)
    return MediaRef(**fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"session_id": "../other"},
        {"turn_id": "wrong"},
        {"kind": "unknown"},
        {"kind": "input", "request_id": "d" * 32},
        {"kind": "tool"},
        {"kind": "tool", "request_id": "d" * 32, "tool_call_id": "x", "tool_name": ""},
        {"kind": "tool", "request_id": "wrong", "tool_call_id": "x", "tool_name": "read_file"},
    ],
)
def test_media_source_does_not_mix_input_tool_or_path_identities(changes):
    with pytest.raises(ValueError):
        source(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"media_id": "../file"},
        {"source": {}},
        {"mime_type": "image/*"},
        {"mime_type": "image/png;charset=utf-8"},
        {"mime_type": "IMAGE/PNG"},
        {"mime_type": "image/png\n"},
        {"size_bytes": -1},
        {"size_bytes": True},
        {"version": "x" * 64},
        {"schema_version": 2},
        {"schema_version": True},
        {"created_at": "2026-11-01"},
        {"created_at": "2026-11-01T00:00:00+08:00"},
        {"created_at": []},
    ],
)
def test_media_reference_rejects_ambiguous_or_unversioned_metadata(changes):
    with pytest.raises(ValueError):
        reference(**changes)


@pytest.mark.parametrize("change", ["extra", "missing", "source_extra", "source_missing", "data"])
def test_decoding_media_reference_never_accepts_paths_or_inline_body(change):
    data = asdict(reference())
    if change == "extra":
        data["path"] = "C:/private/file.png"
    elif change == "missing":
        del data["created_at"]
    elif change == "source_extra":
        data["source"]["authority"] = "admin"
    elif change == "source_missing":
        del data["source"]["kind"]
    else:
        data["data"] = "base64-encoded-by-model"
    with pytest.raises(ValueError):
        decode_media_reference(data)


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "f" * 8},
        {"turn_id": "f" * 32},
        {"request_id": "f" * 32},
        {"tool_call_id": "missing"},
        {"tool_name": "other_tool"},
    ],
)
def test_invalid_tool_source_is_rejected_before_binary_creation(tmp_path, change):
    with started_tool(tmp_path / "sessions") as (store, sid, link, _):
        origin = MediaSource(
            sid, link["turn_id"], "tool", link["request_id"], link["tool_call_id"], "read_file"
        )
        forged = replace(origin, **change)
        media = SessionMedia(store, MediaStore(store.root / "_media"))
        before = store.load_events(sid)
        with pytest.raises(StorageError):
            media.publish(forged, b"abc", "application/octet-stream")
        assert store.load_events(sid) == before
        assert not media.files.root.exists()


def test_multiple_media_per_tool_keep_distinct_ids_and_reject_finished_source(tmp_path):
    with started_tool(tmp_path / "sessions") as (store, sid, link, _):
        origin = MediaSource(
            sid, link["turn_id"], "tool", link["request_id"], link["tool_call_id"], "read_file"
        )
        first = reference(source=origin)
        second = replace(first, media_id="d" * 32)
        store.append_media(first)
        store.append_media(second)
        events = store.load_events(sid)
        with pytest.raises(ValueError):
            store.append_media(first)
        assert store.load_events(sid) == events
        store.append_tool_event(sid, "tool_finished", {**link, "output": json.dumps({"ok": True})})
        with pytest.raises(ValueError):
            store.append_media(replace(first, media_id="e" * 32))
        assert store.load_media(sid, first.media_id) == first
        assert store.load_media(sid, second.media_id) == second


def test_input_media_cannot_be_attached_after_a_model_request(tmp_path):
    from adam.session.storage import SessionStore

    with SessionStore(tmp_path / "sessions") as store:
        sid = store.create_session("input", "")
        turn = store.start_turn(sid, "selected local file")
        media = SessionMedia(store, MediaStore(store.root / "_media"))
        first = media.publish(MediaSource(sid, turn, "input"), b"abc", "application/octet-stream")
        store.start_request(sid, turn, "test")
        before = store.load_events(sid)
        with pytest.raises(StorageError):
            media.publish(MediaSource(sid, turn, "input"), b"new", "application/octet-stream")
        assert store.load_events(sid) == before
        assert media.read(first.media_id, expected_version=first.version).data == b"abc"


@pytest.mark.parametrize(
    "fault",
    [
        "magic",
        "header_length",
        "oversized_header",
        "short_metadata",
        "json",
        "duplicate_field",
        "unknown_version",
        "size",
        "source",
        "tail",
    ],
)
def test_binary_container_rejects_bad_or_oversized_disk_fields(tmp_path, fault):
    files = MediaStore(tmp_path, max_bytes=32)
    item = files.save(source(), b"abc", "application/octet-stream")
    path = tmp_path / item.source.session_id / (item.media_id + ".media")
    header = asdict(item)
    magic = b"ADAM-MEDIA\x00\x01\n"
    if fault == "magic":
        encoded = b"unsupported"
    elif fault == "header_length":
        encoded = magic + b"\x00"
    elif fault == "oversized_header":
        encoded = magic + (32769).to_bytes(4, "big")
    elif fault == "short_metadata":
        # 即使残缺头碰巧是有效的空正文引用，也不能忽略声明长度未读满。
        header.update(size_bytes=0, version=hashlib.sha256(b"").hexdigest())
        data = json.dumps(header).encode()
        encoded = magic + (len(data) + 1).to_bytes(4, "big") + data
    else:
        if fault == "unknown_version":
            header["schema_version"] = 2
        elif fault == "size":
            header["size_bytes"] = 30
        elif fault == "source":
            header["source"]["session_id"] = "d" * 8
        data = json.dumps(header).encode()
        if fault == "json":
            data = b"not-json"
        elif fault == "duplicate_field":
            data = data[:-1] + b',"size_bytes":3}'
        encoded = (
            magic + len(data).to_bytes(4, "big") + data + (b"abc" if fault != "tail" else b"x" * 33)
        )
    path.write_bytes(encoded)
    with pytest.raises(StorageError):
        files.read(item.source.session_id, item.media_id, expected_version=item.version)


def test_same_id_never_overwrites_and_old_content_version_is_required(tmp_path):
    files = MediaStore(tmp_path)
    first = files.save(source(), b"abc", "application/octet-stream", media_id="c" * 32)
    before = (tmp_path / first.source.session_id / (first.media_id + ".media")).read_bytes()
    with pytest.raises(StorageError):
        files.save(source(), b"new", "application/octet-stream", media_id=first.media_id)
    assert (tmp_path / first.source.session_id / (first.media_id + ".media")).read_bytes() == before
    with pytest.raises(StorageError, match="版本"):
        files.read(first.source.session_id, first.media_id, expected_version="e" * 64)


def test_corrupt_published_item_does_not_free_session_quota(tmp_path):
    files = MediaStore(tmp_path, max_session_bytes=8)
    first = files.save(source(), b"1234567", "application/octet-stream")
    path = tmp_path / first.source.session_id / (first.media_id + ".media")
    path.write_bytes(b"corrupt")
    with pytest.raises(StorageError):
        files.save(source(), b"x", "application/octet-stream")
    assert list(path.parent.glob("*.media")) == [path]
    assert path.read_bytes() == b"corrupt"


def test_link_resolution_into_another_owner_is_rejected(tmp_path, monkeypatch):
    files = MediaStore(tmp_path / "root")
    original = type(files.root).resolve
    other = tmp_path / "other-owner"

    def redirected(path, *args, **kwargs):
        # 模拟系统真实路径解析边界；不依赖 Windows 创建目录链接的额外权限。
        if path == files.root:
            return other
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(files.root), "resolve", redirected)
    with pytest.raises(StorageError, match="重定向"):
        files.save(source(), b"abc", "application/octet-stream")
    assert not other.exists() and not files.root.exists()


@pytest.mark.parametrize(
    "limits",
    [
        {"max_bytes": 0},
        {"max_bytes": True},
        {"max_bytes": 100000001},
        {"max_session_bytes": -1},
        {"max_items": 4097},
    ],
)
def test_binary_storage_resource_limits_are_bounded(tmp_path, limits):
    with pytest.raises(ValueError):
        MediaStore(tmp_path, **limits)
