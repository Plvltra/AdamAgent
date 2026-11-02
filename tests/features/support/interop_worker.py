"""真实独立进程验收宿主；只处理文本计数，不接触用户会话或模型。"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from threading import Thread

from adam.interop.client import TextAgentClient
from adam.interop.server import create_server


def main() -> None:
    """服务进程写入就绪端口，客户端进程输出实际调用结果及双方 PID。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("server", "client"))
    parser.add_argument("value")
    args = parser.parse_args()
    if args.mode == "client":
        client = TextAgentClient(args.value)
        print(
            json.dumps(
                {
                    "client_pid": os.getpid(),
                    "card": asdict(client.discover()),
                    "result": asdict(client.send("one two three")),
                }
            )
        )
        return

    def count_words(text: str) -> str:
        """计算真实输入，并用本进程 PID 证明回调不在客户端运行。"""
        if text == "fail":
            raise ValueError("private failure detail")
        return json.dumps({"words": len(text.split()), "server_pid": os.getpid()})

    with create_server(count_words) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            Path(args.value).write_text(
                json.dumps({"port": server.server_port, "pid": os.getpid()}), encoding="utf-8"
            )
            sys.stdin.readline()
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
