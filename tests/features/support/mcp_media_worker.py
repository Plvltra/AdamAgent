"""独立进程在同一屏障后写入媒体缓存，验收跨进程锁与重复引用。"""

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from adam.mcp.media_store import MCPMediaStore, MediaSource, prepare_media


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    root = Path(job["root"])
    source = MediaSource(**job["source"])
    item = prepare_media(job["blob"], job["mime_type"], source)
    (root.parent / f"{os.getpid()}.ready").touch()
    deadline = time.monotonic() + 15
    while not (root.parent / "release").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("parent did not release media writers")
        time.sleep(0.01)
    store = MCPMediaStore(root, max_items=1)
    result = store.save_many([item])[0]
    assert store.read(result) == item.data
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
