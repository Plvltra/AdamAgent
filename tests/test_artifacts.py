"""附件格式与参数边界；业务保存和重启场景放在 tests/features/test_artifact_storage.py。"""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from adam.contracts.artifacts import ArtifactSource
from adam.contracts.errors import StorageError
from adam.session.artifacts import ArtifactStore


@pytest.fixture
def stored(tmp_path):
    store = ArtifactStore(tmp_path, max_bytes=64, max_read_chars=8)
    source = ArtifactSource("a" * 8, "b" * 32, "c" * 32, "tool-1", "read_file")
    reference = store.save(source, "中文 and text")
    path = tmp_path / source.session_id / f"{reference.artifact_id}.json"
    return store, source, reference, path


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "../other"),
        ("turn_id", "short"),
        ("request_id", "G" * 32),
        ("tool_call_id", ""),
        ("tool_call_id", "中" * 400),
        ("tool_name", "bad\0name"),
    ],
)
def test_source_rejects_invalid_identity_before_storage(stored, field, value):
    _, source, _, _ = stored
    with pytest.raises(ValueError):
        replace(source, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_bytes", 0),
        ("max_session_bytes", True),
        ("max_items", 4097),
        ("max_read_chars", -1),
    ],
)
def test_storage_rejects_invalid_limits(tmp_path, field, value):
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path, **{field: value})


@pytest.mark.parametrize(
    ("offset", "count"),
    [(-1, 2), (True, 2), (1000, 2), (0, 0), (0, 9), (0, False)],
)
def test_invalid_page_bounds_never_change_the_file(stored, offset, count):
    store, source, reference, path = stored
    original = path.read_bytes()
    with pytest.raises(ValueError):
        store.read(
            source.session_id,
            reference.artifact_id,
            expected_version=reference.version,
            offset=offset,
            count=count,
        )
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "damage",
    [
        "bad_utf8",
        "half_json",
        "duplicate",
        "future",
        "bool_version",
        "foreign_session",
        "foreign_artifact",
        "wrong_content",
        "wrong_size",
        "wrong_characters",
        "false_complete",
        "unknown_field",
        "invalid_surrogate",
        "oversized",
    ],
)
def test_bad_attachment_is_rejected_without_repair(stored, damage):
    store, source, reference, path = stored
    payload = json.loads(path.read_bytes())
    if damage == "bad_utf8":
        raw = b"\xff"
    elif damage == "half_json":
        raw = b'{"format_version":'
    elif damage == "duplicate":
        raw = path.read_bytes().replace(
            b'"format_version": 1', b'"format_version": 1, "format_version": 1'
        )
    elif damage == "oversized":
        raw = b" " * (64 * 6 + 65537)
    else:
        if damage == "future":
            payload["format_version"] = 2
        elif damage == "bool_version":
            payload["format_version"] = True
        elif damage == "foreign_session":
            payload["reference"]["source"]["session_id"] = "d" * 8
        elif damage == "foreign_artifact":
            payload["reference"]["artifact_id"] = "e" * 32
        elif damage == "wrong_content":
            payload["text"] = "different"
        elif damage == "wrong_size":
            payload["reference"]["saved_bytes"] = 1
        elif damage == "wrong_characters":
            payload["reference"]["saved_chars"] -= 1
        elif damage == "false_complete":
            payload["reference"]["complete"] = False
        elif damage == "unknown_field":
            payload["surprise"] = 1
        elif damage == "invalid_surrogate":
            payload["text"] = "\ud800"
        raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    with pytest.raises(StorageError, match="损坏"):
        store.describe(source.session_id, reference.artifact_id)
    assert path.read_bytes() == raw


def test_invalid_text_and_path_ids_do_not_publish_new_data(stored):
    store, source, reference, path = stored
    with pytest.raises(StorageError, match="UTF-8"):
        store.save(source, "\ud800")
    with pytest.raises(ValueError):
        store.save(source, "text", artifact_id="../other")
    with pytest.raises(ValueError):
        store.describe("../other", reference.artifact_id)
    with pytest.raises(FrozenInstanceError):
        reference.complete = False
    assert list(path.parent.glob("*.json")) == [path]
