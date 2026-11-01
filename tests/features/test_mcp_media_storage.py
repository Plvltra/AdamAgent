"""B15.3.2.1：真实二进制持久引用、配额、损坏/故障及独立进程竞争。"""

import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from adam.contracts.errors import ToolError
from adam.mcp.media_store import (
    MAX_MEDIA_BYTES,
    MCPMediaStore,
    MediaSource,
    PreparedMedia,
    decode_reference,
    prepare_media,
)


def item(data=b"\x00\xff\x01\x80", *, source=None, mime="application/octet-stream"):
    source = source or MediaSource("server", "a" * 64, "tool", "read_binary", 0)
    return prepare_media(base64.b64encode(data).decode(), mime, source)


def test_saved_reference_restores_actual_bytes_after_restart_and_binds_source(tmp_path):
    prepared = item()
    store = MCPMediaStore(tmp_path / "media")
    assert not store.root.exists()
    assert "data" not in repr(prepared) and "data" not in asdict(prepared.reference)
    refs = store.save_many([prepared, prepared])
    assert refs == [prepared.reference] * 2 and len(list(store.root.glob("*.bin"))) == 1
    serialized = json.loads(json.dumps(asdict(refs[0])))
    assert MCPMediaStore(store.root).read(serialized) == prepared.data
    original = next(store.root.glob("*.bin"))
    before = original.stat().st_mtime_ns
    assert store.save_many([prepared])[0] == refs[0]
    assert original.stat().st_mtime_ns == before
    source = replace(prepared.reference.source, server_id="b" * 64)
    other = item(source=source)
    assert other.reference.media_id != refs[0].media_id
    assert other.reference.version == refs[0].version
    assert item(mime="image/png").reference.media_id != refs[0].media_id


@pytest.mark.parametrize(
    "blob",
    ["%%%", "a", "YWJj\n", "秘密", "x" * 1398105],
    ids=["characters", "padding", "whitespace", "non_ascii", "oversized"],
)
def test_invalid_or_excess_encoded_media_fails_before_io(blob, tmp_path):
    with pytest.raises(ToolError):
        prepare_media(blob, None, item().reference.source)
    assert list(tmp_path.iterdir()) == []


def test_exact_byte_limit_and_empty_data_are_complete_but_overflow_is_not_truncated(tmp_path):
    exact = item(b"x" * MAX_MEDIA_BYTES)
    empty = item(b"")
    store = MCPMediaStore(tmp_path / "media")
    store.save_many([exact, empty])
    assert len(store.read(exact.reference)) == MAX_MEDIA_BYTES
    assert store.read(empty.reference) == b""
    with pytest.raises(ToolError, match="1 MiB"):
        item(b"x" * (MAX_MEDIA_BYTES + 1))
    assert len(list(store.root.glob("*.bin"))) == 2


@pytest.mark.parametrize(
    "fault", ["unknown", "missing", "path", "source", "version", "size", "mime"]
)
def test_changed_reference_is_not_a_path_or_authority_to_read_other_content(tmp_path, fault):
    prepared = item()
    store = MCPMediaStore(tmp_path / "media")
    store.save_many([prepared])
    value = asdict(prepared.reference)
    if fault == "unknown":
        value["path"] = "outside"
    elif fault == "missing":
        del value["format"]
    elif fault == "path":
        value["media_id"] = "../outside"
    elif fault == "source":
        value["source"]["name"] = "other_source"
    elif fault == "version":
        value["version"] = "c" * 64
    elif fault == "size":
        value["size_bytes"] = True
    else:
        value["mime_type"] = "image/png"
    with pytest.raises(ToolError):
        store.read(value)
    assert store.read(prepared.reference) == prepared.data


@pytest.mark.parametrize("fault", ["modify", "remove", "directory"])
def test_missing_or_corrupted_saved_bytes_never_return_success(tmp_path, fault):
    prepared = item()
    store = MCPMediaStore(tmp_path / "media")
    store.save_many([prepared])
    path = next(store.root.glob("*.bin"))
    if fault == "modify":
        path.write_bytes(b"corrupted")
    else:
        path.unlink()
        if fault == "directory":
            path.mkdir()
    with pytest.raises(ToolError):
        MCPMediaStore(store.root).read(prepared.reference)


def test_batch_preflight_counts_deduplication_and_never_evicts_existing_references(tmp_path):
    first, second = item(b"first"), item(b"second")
    store = MCPMediaStore(tmp_path / "media", max_items=1, max_total_bytes=6)
    with pytest.raises(ToolError, match="配额"):
        store.save_many([first, second])
    assert not list(store.root.glob("*.bin"))
    store.save_many([first, first])
    with pytest.raises(ToolError, match="配额"):
        store.save_many([second])
    assert store.read(first.reference) == b"first"
    assert len(list(store.root.glob("*.bin"))) == 1
    bounded = MCPMediaStore(tmp_path / "small", max_total_bytes=4)
    with pytest.raises(ToolError, match="配额"):
        bounded.save_many([first])
    assert not list(bounded.root.glob("*.bin"))


def test_interrupted_temporary_files_count_against_quota_but_are_not_promoted(tmp_path):
    store = MCPMediaStore(tmp_path / "media", max_total_bytes=6)
    store.root.mkdir()
    orphan = store.root / ".pending-interrupted"
    orphan.write_bytes(b"oldtmp")
    with pytest.raises(ToolError, match="配额"):
        store.save_many([item()])
    assert orphan.read_bytes() == b"oldtmp" and not list(store.root.glob("*.bin"))
    with pytest.raises(ToolError):
        store.read(item().reference)


@pytest.mark.parametrize("boundary", ["sync", "replace"])
def test_publish_failure_preserves_old_bytes_and_leaves_no_returned_new_reference(
    tmp_path, monkeypatch, boundary
):
    store = MCPMediaStore(tmp_path / "media")
    original, new = item(b"original"), item(b"new")
    store.save_many([original])

    def fail(*args, **kwargs):
        raise OSError("injected media publication failure")

    monkeypatch.setattr(
        "adam.mcp.media_store.os." + ("fsync" if boundary == "sync" else "replace"), fail
    )
    with pytest.raises(ToolError, match="操作失败"):
        store.save_many([new])
    assert store.read(original.reference) == b"original"
    assert not list(store.root.glob(".pending-*"))
    with pytest.raises(ToolError):
        store.read(new.reference)


def test_invalid_prepared_data_rejects_entire_batch_before_creating_cache(tmp_path):
    prepared = item()
    invalid = PreparedMedia(prepared.reference, b"forged bytes")
    store = MCPMediaStore(tmp_path / "media")
    with pytest.raises(ToolError, match="内容与引用不符"):
        store.save_many([prepared, invalid])
    assert not store.root.exists()


def test_two_independent_processes_publish_one_immutable_reference(tmp_path):
    prepared = item()
    root = tmp_path / "media"
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            {
                "root": str(root),
                "source": asdict(prepared.reference.source),
                "blob": base64.b64encode(prepared.data).decode(),
                "mime_type": prepared.reference.mime_type,
            }
        ),
        encoding="utf-8",
    )
    workers = []
    try:
        for _ in range(2):
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-m", "tests.features.support.mcp_media_worker", str(job)],
                    cwd=Path(__file__).resolve().parents[2],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("*.ready"))) < 2:
            assert all(worker.poll() is None for worker in workers)
            assert time.monotonic() < deadline
            time.sleep(0.02)
        (tmp_path / "release").touch()
        for worker in workers:
            output, error = worker.communicate(timeout=10)
            assert worker.returncode == 0, error
            assert decode_reference(json.loads(output)) == prepared.reference
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate(timeout=10)
    assert len(list(root.glob("*.bin"))) == 1
    assert MCPMediaStore(root).read(prepared.reference) == prepared.data
