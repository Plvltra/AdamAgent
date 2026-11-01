"""独立百科搜索入口；不创建模型或会话，不把摘要当成已读正文。"""

import argparse
import json
from dataclasses import asdict

from adam.contracts.errors import RequestInterrupted
from adam.web.models import WebError
from adam.web.policy import NetworkPolicy
from adam.web.runtime import open_search
from adam.web.search_models import SearchLimits


def main() -> int:
    """显式选择后端并输出候选 JSON；缺少配置与空结果可区分，失败不重试。"""
    parser = argparse.ArgumentParser(
        description="Search bounded encyclopedia candidates, not pages"
    )
    parser.add_argument("query")
    parser.add_argument("--backend", choices=("wikipedia",))
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        limits = SearchLimits(max_results=args.limit)
    except ValueError as error:
        parser.error(str(error))
    try:
        with open_search(args.backend, limits, NetworkPolicy()) as backend:
            result = backend.search(args.query)
        print(json.dumps({"ok": True, **asdict(result)}, ensure_ascii=False))
        return 0
    except WebError as error:
        print(
            json.dumps(
                {"ok": False, "error_type": error.kind, "error": str(error)}, ensure_ascii=False
            )
        )
        return 1
    except RequestInterrupted:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
