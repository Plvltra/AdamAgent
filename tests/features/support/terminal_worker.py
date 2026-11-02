"""B30.3：真实输入已送出但接受回执尚未保存时，等待父测试强杀。"""

import json
import sys
import time
from pathlib import Path

from adam.application.terminals import TerminalService
from tests.features.test_terminal_persistence import make_service


def run(root):
    service = make_service(root, lambda _: True)
    owner = "1234abcd"
    script = (
        "import sys,time; from pathlib import Path; data=sys.stdin.readline(); "
        "Path('executed').write_text(data); end=time.monotonic()+15\n"
        "while not Path('release').exists() and time.monotonic()<end: time.sleep(.02)\n"
        "Path('child-done').touch()"
    )
    identity = service.start(
        owner, {"argv": [sys.executable, "-u", "-c", script]}, {"turn_id": "turn"}
    )["terminal_id"]
    original = TerminalService._save

    def save(self, sid, record):
        if record["inputs"] and record["inputs"][-1]["status"] == "accepted":
            deadline = time.monotonic() + 5
            while not (root / "executed").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (root / "executed").exists()
            (root / "ready.tmp").write_text(json.dumps({"terminal_id": identity}), encoding="utf-8")
            (root / "ready.tmp").replace(root / "ready.json")
            while True:
                time.sleep(0.1)
        return original(self, sid, record)

    TerminalService._save = save
    service.send(owner, identity, "once\n", {"turn_id": "turn", "tool_call_id": "input"})


if __name__ == "__main__":
    run(Path(sys.argv[1]))
