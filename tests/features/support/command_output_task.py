"""真实长输出进程及子进程；就绪之后等待外部取消或期限，不自行报成功。"""

import os
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
Path("pids").write_text(f"{os.getpid()},{child.pid}", encoding="utf-8")
with Path("executed").open("a", encoding="utf-8") as marker:
    marker.write("x")
sys.stdout.buffer.write(("BEGIN-A\n" + "汉😀\n" * 5000 + "CANCEL-END-A").encode())
sys.stdout.buffer.flush()
sys.stderr.buffer.write(("BEGIN-B\n" + "warning\n" * 5000 + "CANCEL-END-B").encode())
sys.stderr.buffer.flush()
Path("ready").touch()
time.sleep(30)
