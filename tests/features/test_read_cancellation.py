"""T6.2 读取取消：真实目录/文件及 Git 进程边界，不使用远端模型。"""

import json
import os
import subprocess
import sys
from threading import Event, Thread

import pytest

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import RequestInterrupted
from adam.tool_calls.factory import create_tool_registry


@pytest.mark.parametrize(
    "name,args",
    [
        ("list_directory", {"path": "."}),
        ("find_files", {"path": ".", "pattern": "*.txt"}),
        ("search_text", {"path": ".", "query": "needle"}),
    ],
)
def test_directory_iteration_cancel_closes_real_iterator(tmp_path, monkeypatch, name, args):
    for index in range(4):
        (tmp_path / f"{index}.txt").write_text("needle")
    cancel = CancellationToken()
    scandir = os.scandir
    closed, visited = [], []

    class InterruptingScan:
        def __init__(self, path):
            self.scan = scandir(path)

        def __enter__(self):
            return self

        def __iter__(self):
            for entry in self.scan:
                visited.append(entry.name)
                cancel.cancel()
                yield entry

        def __exit__(self, *error):
            self.scan.close()
            closed.append(True)

    registry = create_tool_registry(tmp_path)
    monkeypatch.setattr(os, "scandir", InterruptingScan)
    with pytest.raises(RequestInterrupted):
        registry.execute(name, json.dumps(args), cancel=cancel)
    assert len(visited) == 1 and closed == [True]


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"path": "large.txt"}),
        ("search_text", {"path": "large.txt", "query": "needle"}),
    ],
)
def test_text_read_cancel_closes_stream_before_another_chunk(tmp_path, monkeypatch, name, args):
    target = tmp_path / "large.txt"
    target.write_bytes(b"needle\n" * 30000)
    cancel = CancellationToken()
    original_open = type(target).open
    reads, streams = [], []

    class InterruptingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def read(self, count):
            reads.append(count)
            result = self.stream.read(count)
            cancel.cancel()
            return result

        def __exit__(self, *error):
            self.stream.close()

    def open_file(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == target and args == ("rb",):
            streams.append(stream)
            return InterruptingReader(stream)
        return stream

    monkeypatch.setattr(type(target), "open", open_file)
    with pytest.raises(RequestInterrupted):
        create_tool_registry(tmp_path).execute(name, json.dumps(args), cancel=cancel)
    assert reads == [65536]
    assert streams and all(stream.closed for stream in streams)


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
def test_git_wait_terminates_real_process_before_return(tmp_path, monkeypatch, failure):
    """仅把 Git 启动边界替换成挂起程序；启动、等待、取消和回收均为真实进程。"""
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    original_popen = subprocess.Popen
    created = Event()
    processes, requested = [], []
    cancel = CancellationToken()

    def start(argv, **kwargs):
        if argv[0] != "git":
            return original_popen(argv, **kwargs)
        requested.append(argv)
        process = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        created.set()
        return process

    def interrupt():
        assert created.wait(4)
        cancel.cancel()

    monkeypatch.setattr(subprocess, "Popen", start)
    controller = Thread(target=interrupt) if failure == "cancel" else None
    if controller:
        controller.start()
    try:
        registry = create_tool_registry(tmp_path, limits=ToolLimits(git_timeout_seconds=1))
        if failure == "cancel":
            with pytest.raises(RequestInterrupted):
                registry.execute("git_log", '{"path":"."}', cancel=cancel)
        else:
            result = json.loads(registry.execute("git_log", '{"path":"."}', cancel=cancel))
            assert result["ok"] is False
        assert requested[0][-3:] == ["-10", "--format=%h %s", "--no-decorate"]
        assert processes and all(process.poll() is not None for process in processes)
    finally:
        cancel.cancel()
        if controller:
            controller.join(timeout=4)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    assert controller is None or not controller.is_alive()
