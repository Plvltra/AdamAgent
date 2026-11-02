"""按当前轮次选择最新检查报告，使用注入的窄接口核对文件版本。"""

from adam.contracts.diagnostic_context import (
    MAX_CONTEXT_BYTES,
    MAX_REPORTS,
    DiagnosticObservation,
    DiagnosticSelection,
    VersionCheck,
    decode_selection,
    latest_reports,
    selection_bytes,
    validate_sources,
)
from adam.contracts.diagnostics import parse_diagnostics
from adam.session.storage import SessionStore


class DiagnosticContextReader:
    """只读自己的 SessionStore；不运行检查器、请求模型、改写日志或扩大文件权限。"""

    def __init__(self, store: SessionStore, check_version: VersionCheck) -> None:
        self.store, self.check_version = store, check_version

    def __call__(self, session_id: str, turn_id: str) -> DiagnosticSelection:
        """优先最近报告并限制总字节；省略旧文件须计数，存储错误与取消继续传播。"""
        hooks = self.store.load_hooks(session_id)
        latest = latest_reports(hooks, turn_id)
        selected: list[DiagnosticObservation] = []
        for item in reversed(latest):
            if len(selected) == MAX_REPORTS:
                break
            report = parse_diagnostics(item["diagnostics"])
            candidate = DiagnosticObservation(
                item["hook_id"],
                item["tool_call_id"],
                report,
                "not_checked" if report.status in {"stale", "unavailable"} else "unavailable",
            )
            trial = DiagnosticSelection(
                session_id, turn_id, tuple([candidate, *selected]), len(latest) - len(selected) - 1
            )
            if selection_bytes(trial.to_record()) > MAX_CONTEXT_BYTES:
                continue
            freshness = (
                "not_checked"
                if report.status in {"stale", "unavailable"}
                else self.check_version(report.path, report.expected_version)
            )
            selected.insert(
                0,
                DiagnosticObservation(candidate.hook_id, candidate.tool_call_id, report, freshness),
            )
        result = decode_selection(
            DiagnosticSelection(
                session_id, turn_id, tuple(selected), len(latest) - len(selected)
            ).to_record()
        )
        validate_sources(result, hooks)
        return result
