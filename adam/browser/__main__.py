"""独立 DOM 观察命令；不创建模型、会话或日常浏览器窗口。"""

import argparse
import json
from dataclasses import asdict

from adam.browser.models import BrowserError
from adam.browser.policy import BrowserPolicy, origin_of
from adam.browser.runtime import open_browser
from adam.contracts.errors import RequestInterrupted


def main() -> int:
    """用户显式指定的 URL origin 是本次唯一许可站点；输出 JSON，失败非零。"""
    parser = argparse.ArgumentParser(
        description="Observe a single-origin page in an isolated headless browser"
    )
    parser.add_argument("url")
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chromium")
    args = parser.parse_args()
    try:
        policy = BrowserPolicy(frozenset({origin_of(args.url)}))
        channel = None if args.channel == "chromium" else args.channel
        with open_browser(policy, channel=channel) as browser:
            result = browser.open(args.url)
        print(json.dumps({"ok": True, **asdict(result)}, ensure_ascii=False))
        return 0
    except BrowserError as error:
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
