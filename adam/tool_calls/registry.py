"""工具参数校验、分发与结果封装；具体执行能力由工厂注册。"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.cancellation import CancellationToken
from adam.contracts.errors import ToolDeferredError, ToolError, ToolHookError, ToolModeError
from adam.contracts.execution import ExecutionMode, ToolCapability, check_tool_mode
from adam.contracts.models import ExecutionRecorder
from adam.tool_calls.discovery import ToolDirectory, validate_groups
from adam.tool_calls.file_evidence import FileResult, FileStateError

ToolHandler = Callable[[dict[str, Any], ExecutionRecorder | None], dict[str, Any]]
ParallelHandler = Callable[[dict[str, Any], CancellationToken], dict[str, Any]]
PreparedAction = Callable[[CancellationToken], dict[str, Any]]
ResultEncoder = Callable[[dict[str, Any]], str]
ArgumentValidator = Callable[[object], None]
PathSelector = Callable[[dict[str, Any]], tuple[Path, ...]]
ExecutionGuard = Callable[[tuple[Path, ...]], None]
BeforeTool = Callable[[str, dict[str, Any], ExecutionMode], None]


class ToolRegistry:
    """持有说明与处理函数，不创建具体工具，也不识别工具名称决定执行方式。"""

    def __init__(
        self,
        specs: dict[str, Any],
        handlers: dict[str, ToolHandler],
        limits: ToolLimits,
        encoders: dict[str, ResultEncoder] | None = None,
        validators: dict[str, ArgumentValidator] | None = None,
        *,
        path_selectors: dict[str, PathSelector] | None = None,
        guard: ExecutionGuard | None = None,
        capabilities: dict[str, ToolCapability] | None = None,
        mode_provider: Callable[[], ExecutionMode] | None = None,
        parallel_handlers: dict[str, ParallelHandler] | None = None,
        groups: dict[str, str] | None = None,
    ) -> None:
        """说明与实现必须一一对应；可注册编码器、校验器和可信能力声明。

        未声明能力按 UNKNOWN 处理；模式提供者只由组装方注入，不接受模型控制。
        groups 仅供发现目录分组，省略时归入 extensions；分组不改变执行能力或权限。
        parallel_handlers 只接收参数和取消令牌，不接收日志回调；必须是可协作取消的无状态读取。
        """
        validate_groups(groups or {}, specs)
        if (
            specs.keys() != handlers.keys()
            or (encoders or {}).keys() - specs.keys()
            or (validators or {}).keys() - specs.keys()
            or (path_selectors or {}).keys() - specs.keys()
            or (capabilities or {}).keys() - specs.keys()
            or any(not isinstance(value, ToolCapability) for value in (capabilities or {}).values())
            or (mode_provider is not None and not callable(mode_provider))
        ):
            raise ValueError("工具说明、处理函数、校验器、能力或模式策略未正确配对。")
        if (parallel_handlers or {}).keys() - specs.keys() or any(
            (capabilities or {}).get(name) is not ToolCapability.READ_ONLY or not callable(handler)
            for name, handler in (parallel_handlers or {}).items()
        ):
            raise ValueError("并发处理函数必须属于本次注册的可信只读工具。")
        self._specs = specs
        self._handlers = handlers
        self.limits = limits
        self._encoders = encoders or {}
        self._validators = validators or {}
        self._path_selectors = path_selectors or {}
        self._guard = guard
        self._capabilities = dict(capabilities or {})
        self._groups = dict(groups or {})
        self._mode_provider = mode_provider
        self._parallel_handlers = dict(parallel_handlers or {})

    def extended(
        self,
        specs: dict[str, Any],
        handlers: dict[str, ToolHandler],
        encoders: dict[str, ResultEncoder] | None = None,
        validators: dict[str, ArgumentValidator] | None = None,
        *,
        path_selectors: dict[str, PathSelector] | None = None,
        capabilities: dict[str, ToolCapability] | None = None,
        parallel_handlers: dict[str, ParallelHandler] | None = None,
        groups: dict[str, str] | None = None,
    ) -> "ToolRegistry":
        """返回扩展后的独立目录；不覆盖同名工具，也不改变已有目录和分组。"""
        validate_groups(groups or {}, specs)
        if (capabilities or {}).keys() - specs.keys() or (
            parallel_handlers or {}
        ).keys() - specs.keys():
            raise ValueError("扩展只能声明本次注册工具的能力。")
        if self._specs.keys() & specs.keys():
            raise ValueError("duplicate tool registration")
        return ToolRegistry(
            {**self._specs, **specs},
            {**self._handlers, **handlers},
            self.limits,
            {**self._encoders, **(encoders or {})},
            {**self._validators, **(validators or {})},
            path_selectors={**self._path_selectors, **(path_selectors or {})},
            guard=self._guard,
            capabilities={**self._capabilities, **(capabilities or {})},
            mode_provider=self._mode_provider,
            parallel_handlers={**self._parallel_handlers, **(parallel_handlers or {})},
            groups={**self._groups, **(groups or {})},
        )

    def guarded(self, guard: ExecutionGuard) -> "ToolRegistry":
        """给整个目录设置执行前检查；保留路径声明，后续扩展也继承检查。"""
        if self._guard is not None:
            raise ValueError("执行前检查已配置。")
        return ToolRegistry(
            self._specs,
            self._handlers,
            self.limits,
            self._encoders,
            self._validators,
            path_selectors=self._path_selectors,
            guard=guard,
            capabilities=self._capabilities,
            mode_provider=self._mode_provider,
            parallel_handlers=self._parallel_handlers,
            groups=self._groups,
        )

    def with_mode(self, provider: Callable[[], ExecutionMode]) -> "ToolRegistry":
        """返回绑定模式读取器的目录；扩展与规则包装保留策略，不能重复替换策略。"""
        if self._mode_provider is not None or not callable(provider):
            raise ValueError("模式策略已配置或读取器无效。")
        return ToolRegistry(
            self._specs,
            self._handlers,
            self.limits,
            self._encoders,
            self._validators,
            path_selectors=self._path_selectors,
            guard=self._guard,
            capabilities=self._capabilities,
            mode_provider=provider,
            parallel_handlers=self._parallel_handlers,
            groups=self._groups,
        )

    def definitions(self) -> list[dict[str, Any]]:
        """返回供 Chat Completions tools 字段使用的工具说明。"""
        return [
            {"type": "function", "function": {"name": name, **spec}}
            for name, spec in self._specs.items()
        ]

    def directory(self) -> ToolDirectory:
        """取得独立的只读说明快照；不携带处理函数、不执行模式提供者或路径检查。"""
        return ToolDirectory(self._specs, self._capabilities, self._groups)

    def capability(self, name: str) -> ToolCapability:
        """返回注册代码声明的能力；未知工具保持 UNKNOWN。"""
        return self._capabilities.get(name, ToolCapability.UNKNOWN)

    def can_parallelize(self, name: str) -> bool:
        """仅显式注册取消接口的只读工具可并行，能力声明本身不授予并发资格。"""
        return name in self._parallel_handlers

    def execute(
        self,
        name: str,
        arguments: str,
        *,
        record_execution: ExecutionRecorder | None = None,
        before_tool: BeforeTool | None = None,
        cancel: CancellationToken | None = None,
    ) -> str:
        """同步校验并执行工具，返回有界 JSON；保留原直接调用入口。

        record_execution 接收命令或编辑状态，普通只读工具不使用它。
        顺序仍为参数、模式、路径规则、前置 Hook、动作及批准；日志错误继续向上传播。
        """
        token = cancel or CancellationToken()
        token.check()
        action = self.prepare(
            name, arguments, record_execution=record_execution, before_tool=before_tool
        )
        token.check()
        return self.encode(name, action(token))

    def prepare(
        self,
        name: str,
        arguments: str,
        *,
        record_execution: ExecutionRecorder | None = None,
        before_tool: BeforeTool | None = None,
    ) -> PreparedAction:
        """在协调线程预检，返回尚未执行的动作；预检拒绝返回失败值，不进入处理函数。

        参数校验后先检查模式和路径规则，再调用 before_tool；批准仍由具体动作处理。
        并行入口不捕获或传递 record_execution。命令、编辑及 Hook 日志故障不能转为普通结果。
        """
        try:
            if name not in self._specs:
                raise ToolError("未知工具。")
            args = json.loads(arguments)
            self._validate(name, args)
            mode = self._mode_provider() if self._mode_provider is not None else ExecutionMode.ACT
            check_tool_mode(mode, self.capability(name))
            if self._guard:
                select = self._path_selectors.get(name)
                self._guard(select(args) if select else ())
            if before_tool:
                before_tool(name, args, mode)
        except (ToolError, OSError, ValueError, subprocess.SubprocessError) as error:
            output = self._failure(error)
            return lambda _cancel: output

        parallel = self._parallel_handlers.get(name)
        if parallel is not None:
            return lambda cancel: self._invoke(lambda: parallel(args, cancel))
        handler = self._handlers[name]
        return lambda _cancel: self._invoke(lambda: handler(args, record_execution))

    def encode(self, name: str, output: dict[str, Any]) -> str:
        """在协调线程编码原动作结果，保留自定义编码器及既有截断语义。"""
        if (
            name in self._encoders
            and "status" in output
            and output["status"] not in {"deferred", "mode_blocked", "hook_blocked"}
        ):
            return self._encoders[name](output)
        encoded = json.dumps(output, ensure_ascii=False)
        if len(encoded) > self.limits.max_result_chars:
            # 保证截断后仍为合法JSON；预览不再作为原始结构解析。
            preview = encoded[: max(0, self.limits.max_result_chars // 3)]
            encoded = json.dumps(
                {
                    "ok": output["ok"],
                    **({"version": output["version"]} if "version" in output else {}),
                    **({"status": output["status"]} if "status" in output else {}),
                    "truncated": True,
                    "preview": preview,
                    "hint": "请缩小范围或分段读取。",
                },
                ensure_ascii=False,
            )
        return encoded

    def _invoke(self, action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """仅转换受控工具故障；取消、日志失败和意外程序错误均向上传播。"""
        try:
            result = action()
            output = {"ok": True, **result}
            return (
                FileResult(output, version=result.version)
                if isinstance(result, FileResult)
                else output
            )
        except (ToolError, OSError, ValueError, subprocess.SubprocessError) as error:
            return self._failure(error)

    @staticmethod
    def _failure(error: Exception) -> dict[str, Any]:
        """模式、规则和 Hook 拒绝保留各自状态，避免误触发自动反思。"""
        statuses = {
            ToolHookError: "hook_blocked",
            ToolModeError: "mode_blocked",
            ToolDeferredError: "deferred",
        }
        for kind, status in statuses.items():
            if isinstance(error, kind):
                return {"ok": False, "status": status, "error": str(error)}
        if isinstance(error, FileStateError):
            return FileResult({"ok": False, "error": str(error)}, version=error.version)
        # 操作系统异常可能包含敏感路径；只回传已控制的业务提示。
        return {
            "ok": False,
            "error": str(error)
            if isinstance(error, ToolError)
            else "工具执行失败：文件编码、访问权限、Git或参数格式无效。",
        }

    def _validate(self, name: str, args: object) -> None:
        """扩展工具可注册完整 Schema 校验器；原生工具保持既有参数契约。"""
        if name in self._validators:
            self._validators[name](args)
            return
        params = self._specs[name]["parameters"]
        if not isinstance(args, dict) or args.keys() - params["properties"].keys():
            raise ToolError("参数必须为对象且不能包含未知字段。")
        if any(key not in args for key in params["required"]):
            raise ToolError("缺少必填参数。")
        types = {"string": str, "integer": int, "boolean": bool, "array": list}
        for key, value in args.items():
            if type(value) is not types[params["properties"][key]["type"]]:
                raise ToolError(f"参数 {key} 类型错误。")
