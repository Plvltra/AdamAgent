"""B40.1：实际 Git 副本隔离、稳定项目身份及清理边界。"""

import json
import subprocess

import pytest

from adam.application.subagents.workspaces import PrivateWorkspaces
from adam.config.settings import PermissionConfig
from adam.contracts.errors import ToolError


def make_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "AGENTS.md").write_text("preserve contract", encoding="utf-8")
    (path / "source.txt").write_text("original", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "core.hooksPath=",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )


def test_private_copies_isolate_files_and_keep_project_identity(tmp_path):
    source = tmp_path / "source"
    make_repo(source)
    service = PrivateWorkspaces(tmp_path / "copies")
    permissions = PermissionConfig(
        additional_read_dirs=(tmp_path / "read",), additional_write_dirs=(tmp_path / "write",)
    )
    first, second = service.create(source, permissions), service.create(source, permissions)
    assert first.memory_identity == first.rules_root == source
    assert first.permissions.additional_read_dirs == permissions.additional_read_dirs
    assert first.permissions.additional_write_dirs == ()
    (first.path / "source.txt").write_text("edited")
    assert (source / "source.txt").read_text() == "original"
    assert (second.path / "source.txt").read_text() == "original"
    assert service.git(first.path, ["remote"]) == ""
    assert service.git(source, ["status", "--porcelain"]) == ""
    service.cleanup(first.copy_id)
    service.cleanup(first.copy_id)
    assert not first.path.exists()
    assert PrivateWorkspaces(service.root).recover_abandoned() == [second.copy_id]
    assert not second.path.exists()
    assert source.is_dir()


def test_dirty_source_and_tampered_cleanup_target_are_rejected(tmp_path):
    source = tmp_path / "source"
    make_repo(source)
    service = PrivateWorkspaces(tmp_path / "copies")
    (source / "source.txt").write_text("dirty")
    with pytest.raises(ToolError, match="提交"):
        service.create(source, PermissionConfig())
    (source / "source.txt").write_text("original")
    copy = service.create(source, PermissionConfig())
    manifest = copy.path.parent / "manifest.json"
    data = json.loads(manifest.read_text())
    data["path"] = str(source)
    manifest.write_text(json.dumps(data))
    with pytest.raises(ToolError, match="身份"):
        service.cleanup(copy.copy_id)
    assert source.is_dir() and copy.path.is_dir()
    data["path"] = str(copy.path)
    manifest.write_text(json.dumps(data))
    service.cleanup(copy.copy_id)
