"""真实管道故障夹具，只代替 WSL 启动器，不用于证明 Linux 权限隔离。"""

import json
import sys


def main():
    phase = sys.argv[1]
    initial = json.loads(sys.stdin.buffer.readline())
    assert initial["argv"] == ["/bin/echo", "hello"]
    if phase == "cancel":
        sys.stdin.buffer.read()
        return 0
    if phase == "partial":
        sys.stdout.write('{"exit_code":')
        sys.stdout.flush()
        return 0
    if phase == "malformed":
        print("bad-json", flush=True)
        return 0
    result = {
        "policy": "bubblewrap-native-single-process-v1",
        "exit_code": 7 if phase == "nonzero" else 0,
        "reason": "exited",
        "stdout": '{"policy":"fake","exit_code":0}',
        "stderr": "actual-error" if phase == "nonzero" else "",
        "captured_bytes": 60,
        "duration_ms": 12,
    }
    if phase == "wrong_policy":
        result["policy"] = "host"
    if phase == "bad_status":
        result["reason"] = "success"
    if phase == "too_large":
        result["stdout"] = "x" * 65537
    print(json.dumps(result), flush=True)
    if phase == "extra":
        print("extra-frame", flush=True)
    return 4 if phase == "launcher_exit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
