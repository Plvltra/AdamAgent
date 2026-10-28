import json
import os
import subprocess

import pytest

from adam.tool_calls import PermissionConfig, ToolLimits, ToolRegistry


def run(registry, name, **args):
    return json.loads(registry.execute(name, json.dumps(args)))


def test_file_tools_paging_search_and_ignore(tmp_path):
    (tmp_path / "a.py").write_text("needle\nsecond\nthird\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("needle", encoding="utf-8")
    tools = ToolRegistry(tmp_path)
    assert run(tools, "list_directory", path=".")["entries"] == [".git", "a.py"]
    found = run(tools, "find_files", path=".", pattern="*.py")
    assert found["results"] == ["a.py"]
    result = run(tools, "read_file", path="a.py", start_line=2, max_lines=1)
    assert result["lines"] == [{"line": 2, "text": "second"}]
    assert result["next_line"] == 3
    assert run(tools, "search_text", path=".", query="needle")["results"][0]["path"] == "a.py"
    assert len(run(tools, "search_text", path=".", query="needle")["results"]) == 1
    assert run(tools, "read_file", path=".git/HEAD")["ok"]
    assert run(tools, "file_info", path="a.py")["size"] > 0


def test_permission_and_parameter_validation(tmp_path):
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    (other / "file.txt").write_text("external", encoding="utf-8")
    (workspace / "llm.toml").write_text("secret", encoding="utf-8")
    tools = ToolRegistry(workspace)
    assert not run(tools, "read_file", path="../other/file.txt")["ok"]
    assert not run(tools, "read_file", path="llm.toml")["ok"]
    assert not run(tools, "read_file", path="missing")["ok"]
    assert not run(tools, "read_file", path=".", max_lines=True)["ok"]
    assert not run(tools, "file_info", path=".", extra=1)["ok"]
    assert not json.loads(tools.execute("read_file", "[]"))["ok"]
    allowed = ToolRegistry(workspace, PermissionConfig(additional_read_dirs=(other,)))
    assert run(allowed, "read_file", path=str(other / "file.txt"))["ok"]


def test_symlink_cannot_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (target / "data.txt").write_text("secret", encoding="utf-8")
    try:
        (workspace / "link").symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Account cannot create symlinks")
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "New-Item -ItemType Junction -Path link -Target ..\\outside | Out-Null",
            ],
            cwd=workspace,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("Account cannot create symlinks or junctions")
    assert not run(ToolRegistry(workspace), "read_file", path="link/data.txt")["ok"]


def test_output_and_scan_limits(tmp_path):
    (tmp_path / "long.txt").write_text("x" * 10000, encoding="utf-8")
    tools = ToolRegistry(tmp_path, limits=ToolLimits(max_result_chars=512))
    result = tools.execute("read_file", '{"path":"long.txt"}')
    assert len(result) <= 512
    assert json.loads(result)["truncated"]
    assert len(json.loads(result)["version"]) == 64
    (tmp_path / "binary").write_bytes(b"\xff\x00")
    assert not run(tools, "read_file", path="binary")["ok"]
    limited = ToolRegistry(tmp_path, limits=ToolLimits(max_scan_entries=1))
    assert not run(limited, "find_files", path=".", pattern="*")["ok"]


def test_git_queries_are_read_only(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    (tmp_path / "a.txt").write_text("before\n", encoding="utf-8")
    git("add", "a.txt")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "core.hooksPath=",
        "commit",
        "-m",
        "initial",
    )
    (tmp_path / "a.txt").write_text("after\n", encoding="utf-8")
    before = git("status", "--porcelain").stdout
    tools = ToolRegistry(tmp_path)
    assert "a.txt" in run(tools, "git_status", path=".")["text"]
    assert "+after" in run(tools, "git_diff", path=".")["text"]
    assert "initial" in run(tools, "git_log", path=".")["text"]
    assert not run(tools, "git_log", path=".", limit=100)["ok"]
    assert not run(tools, "git_diff", path=".", command="reset --hard")["ok"]
    assert git("status", "--porcelain").stdout == before


def test_git_diff_cannot_read_denied_files_or_run_filters(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init")
    (tmp_path / "llm.toml").write_text("secret", encoding="utf-8")
    git("add", "llm.toml")
    tools = ToolRegistry(tmp_path)
    result = run(tools, "git_diff", path=".", staged=True)
    assert not result["ok"]
    assert "secret" not in json.dumps(result)
    git("config", "filter.demo.clean", "echo should-not-execute")
    assert not run(tools, "git_status", path=".")["ok"]
