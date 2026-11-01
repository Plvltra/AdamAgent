"""隔离宿主通信验收的真实管道进程；仅模拟可信 Linux 启动器，不声称 OS 隔离。"""

import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


def main():
    phase = sys.argv[1]
    initial = json.loads(sys.stdin.buffer.readline())
    assert initial["kind"] == "start"
    if phase == "eof":
        return 7
    if phase == "cancel":
        sys.stdin.buffer.read()
        return 0
    if phase == "malformed":
        print("not-json", flush=True)
        sys.stdin.buffer.read()
        return 0
    if phase == "partial":
        sys.stdout.write('{"kind":')
        sys.stdout.flush()
        return 0
    send({"kind": "call", "sequence": 1, "name": "file_info", "arguments": {"path": "a.txt"}})
    raw = sys.stdin.buffer.readline()
    if not raw:
        return 0
    reply = json.loads(raw)
    assert reply["kind"] == "reply" and reply["sequence"] == 1
    send(
        {
            "kind": "finished",
            "exit_code": 0,
            "reason": "exited",
            "result": {"count": 1},
            "has_result": True,
            "calls": 1,
            "stderr": "",
        }
    )
    if phase == "extra":
        print("unexpected trailing output", flush=True)
    return 7 if phase == "launcher_exit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
