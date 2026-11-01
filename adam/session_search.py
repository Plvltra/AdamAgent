"""独立的旧会话只读检索入口；显式提供会话范围，不加载聊天 API 配置。"""

import argparse
import json
import sys
from pathlib import Path

from adam.session.search import SessionSearchIndex


def main(argv: list[str] | None = None) -> int:
    """打印可追溯原文和覆盖问题；部分来源不可读返回 2，入口值错误返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--sessions-dir", type=Path, required=True)
    parser.add_argument("--session", dest="sessions", action="append", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--include-diagnostics", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = SessionSearchIndex(args.sessions_dir, tuple(args.sessions)).search(
            args.query, limit=args.limit, include_diagnostics=args.include_diagnostics
        )
    except (OSError, ValueError):
        print("会话检索参数或目录无效；没有恢复或改写任何会话。", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 2 if result.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
