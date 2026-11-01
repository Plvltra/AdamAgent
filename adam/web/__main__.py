"""独立网页读取入口；不会创建模型客户端或会话。"""

import argparse
import json
from dataclasses import asdict

from adam.contracts.errors import RequestInterrupted
from adam.web.adapters.http import HttpPageTransport
from adam.web.models import FetchLimits, WebError
from adam.web.policy import NetworkPolicy
from adam.web.service import WebReader


def main() -> int:
    """输出有来源的 JSON；失败返回非零，不提供内网放行命令行参数。"""
    parser = argparse.ArgumentParser(description="Fetch a bounded public web page with provenance")
    parser.add_argument("url")
    args = parser.parse_args()
    limits, policy = FetchLimits(), NetworkPolicy()
    transport = HttpPageTransport(policy, limits)
    try:
        page = WebReader(transport, policy, limits).fetch(args.url)
        print(json.dumps({"ok": True, **asdict(page)}, ensure_ascii=False))
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
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
