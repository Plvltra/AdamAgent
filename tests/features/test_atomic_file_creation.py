"""B22.2.2：真实硬链接发布完整候选，验证最后一刻同名竞争和发布失败。"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import adam.tool_calls.edit_tools as edit_module
from adam.contracts.errors import ToolError
from adam.tool_calls.edit_plan import EditConflictError
from tests.features.test_file_creation_approval import planner


@pytest.mark.parametrize(
    "content", ["", "中文\r\nlast", "a" * 65536], ids=["empty", "utf8_crlf", "64k"]
)
def test_only_complete_candidate_becomes_visible_and_temporary_name_is_removed(
    tmp_path, monkeypatch, content
):
    tool = planner(tmp_path, lambda _: True)
    plan = tool.prepare_create({"path": "new.txt", "content": content})
    assert tool.confirm_create(plan)
    real_link, real_fsync = edit_module.os.link, edit_module.os.fsync
    observed = []

    def synced(descriptor):
        assert not plan.path.exists()
        observed.append("fsync")
        real_fsync(descriptor)

    def publish(source, target):
        assert observed == ["fsync"] and not plan.path.exists()
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes() == plan.candidate
        observed.append("publish")
        real_link(source, target)

    monkeypatch.setattr(edit_module.os, "fsync", synced)
    monkeypatch.setattr(edit_module.os, "link", publish)
    monkeypatch.setattr(
        edit_module.os, "replace", lambda *_: pytest.fail("create overwrote target")
    )
    tool.apply_create(plan)
    assert observed == ["fsync", "publish"]
    assert plan.path.read_bytes() == content.replace("\r\n", "\n").encode()
    assert list(tmp_path.iterdir()) == [plan.path]


@pytest.mark.parametrize("directory", [False, True])
def test_last_moment_existing_entry_causes_real_kernel_conflict(tmp_path, monkeypatch, directory):
    tool = planner(tmp_path, lambda _: True)
    plan = tool.prepare_create({"path": "new.txt", "content": "our candidate"})
    assert tool.confirm_create(plan)
    real_link = edit_module.os.link

    def race(source, target):
        # 发生在两次缺失检查之后，真正调用os.link验证内核拒绝覆盖，而非只模拟异常。
        if directory:
            target.mkdir()
        else:
            target.write_bytes(b"another writer")
        real_link(source, target)

    monkeypatch.setattr(edit_module.os, "link", race)
    with pytest.raises(EditConflictError, match="未覆盖"):
        tool.apply_create(plan)
    assert list(tmp_path.iterdir()) == [plan.path]
    assert plan.path.is_dir() if directory else plan.path.read_bytes() == b"another writer"


@pytest.mark.parametrize("boundary", ["write", "flush", "fsync", "link", "cancel"])
def test_failure_before_publication_keeps_target_absent_and_cleans_own_temporary(
    tmp_path, monkeypatch, boundary
):
    tool = planner(tmp_path)
    plan = tool.prepare_create({"path": "new.txt", "content": "candidate"})
    original_temp = edit_module.tempfile.NamedTemporaryFile

    def fail(*_args):
        if boundary == "cancel":
            raise KeyboardInterrupt
        raise OSError("injected publication boundary failure")

    @contextmanager
    def failed_stream(*args, **kwargs):
        with original_temp(*args, **kwargs) as stream:
            yield SimpleNamespace(
                name=stream.name,
                write=fail if boundary == "write" else stream.write,
                flush=fail if boundary == "flush" else stream.flush,
                fileno=stream.fileno,
            )

    if boundary in {"write", "flush"}:
        monkeypatch.setattr(edit_module.tempfile, "NamedTemporaryFile", failed_stream)
    else:
        monkeypatch.setattr(edit_module.os, "fsync" if boundary == "cancel" else boundary, fail)
    monkeypatch.setattr(edit_module.os, "replace", lambda *_: pytest.fail("unsafe fallback"))
    with pytest.raises(KeyboardInterrupt if boundary == "cancel" else ToolError):
        tool.apply_create(plan)
    assert list(tmp_path.iterdir()) == []


def test_existing_target_before_apply_is_preserved_without_temporary_or_publication(
    tmp_path, monkeypatch
):
    tool = planner(tmp_path)
    plan = tool.prepare_create({"path": "new.txt", "content": "ours"})
    plan.path.write_bytes(b"theirs")
    monkeypatch.setattr(
        edit_module.tempfile, "NamedTemporaryFile", lambda **_: pytest.fail("unneeded write")
    )
    with pytest.raises(ToolError, match="已经存在"):
        tool.apply_create(plan)
    assert plan.path.read_bytes() == b"theirs"


def test_cleanup_failure_after_publication_keeps_result_and_does_not_chmod_target(
    tmp_path, monkeypatch
):
    tool = planner(tmp_path)
    plan = tool.prepare_create({"path": "new.txt", "content": "created"})
    real_unlink = Path.unlink

    def no_cleanup(path, *args, **kwargs):
        if path.name.startswith(".adam-create-"):
            raise OSError("injected cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", no_cleanup)
    monkeypatch.setattr(
        edit_module.os, "chmod", lambda *_: pytest.fail("shared file attributes changed")
    )
    tool.apply_create(plan)
    assert plan.path.read_bytes() == b"created"
    temporary = list(tmp_path.glob(".adam-create-*"))
    assert len(temporary) == 1 and temporary[0].samefile(plan.path)
