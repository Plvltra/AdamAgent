"""保留 python -m adam.diagnostics 入口；只读诊断由 session 包实现。"""

from adam.session.diagnostics import main

if __name__ == "__main__":
    raise SystemExit(main())
