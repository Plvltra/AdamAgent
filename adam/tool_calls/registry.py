"""工具参数校验、分发与结果封装；具体执行能力由工厂注册。"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from adam.config.settings import ToolLimits
from adam.contracts.errors import ToolDeferredError, ToolError
from adam.contracts.models import ExecutionRecorder

ToolHandler = Callable[[dict[str, Any], ExecutionRecorder | None], dict[str, Any]]
ResultEncoder = Callable[[dict[str, Any]], str]
ArgumentValidator = Callable[[object], None]
PathSelector = Callable[[dict[str, Any]], tuple[Path, ...]]
ExecutionGuard = Callable[[tuple[Path, ...]], None]


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
    ) -> None:
        """说明与实现必须一一对应；可按工具注册结果编码器和完整参数校验器。"""
        if (
            specs.keys() != handlers.keys()
            or (encoders or {}).keys() - specs.keys()
            or (validators or {}).keys() - specs.keys()
            or (path_selectors or {}).keys() - specs.keys()
        ):
            raise ValueError("工具说明、处理函数、编码器或校验器未正确配对。")
        self._specs = specs
        self._handlers = handlers
        self.limits = limits
        self._encoders = encoders or {}
        self._validators = validators or {}
        self._path_selectors = path_selectors or {}
        self._guard = guard

    def extended(
        self,
        specs: dict[str, Any],
        handlers: dict[str, ToolHandler],
        encoders: dict[str, ResultEncoder] | None = None,
        validators: dict[str, ArgumentValidator] | None = None,
        *,
        path_selectors: dict[str, PathSelector] | None = None,
    ) -> "ToolRegistry":
        """返回扩展后的独立目录；不覆盖同名工具，也不改变已有目录。"""
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
        )

    def definitions(self) -> list[dict[str, Any]]:
        """返回供 Chat Completions tools 字段使用的工具说明。"""
        return [
            {"type": "function", "function": {"name": name, **spec}}
            for name, spec in self._specs.items()
        ]

    def execute(
        self, name: str, arguments: str, *, record_execution: ExecutionRecorder | None = None
    ) -> str:
        """校验 JSON 参数并执行工具，返回有界 JSON 字符串；工具错误作为结果回传。

        record_execution 接收命令或编辑的状态事件，普通只读工具忽略该参数。
        参数校验后先运行注入的检查，再进入工具处理与批准流程。
        CommandLogError / EditLogError 不转换为普通结果，调用方应停止以免自动重试。
        """
        try:
            if name not in self._specs:
                raise ToolError("未知工具。")
            args = json.loads(arguments)
            self._validate(name, args)
            if self._guard:
                select = self._path_selectors.get(name)
                self._guard(select(args) if select else ())
            result = self._handlers[name](args, record_execution)
            output = {"ok": True, **result}
        except ToolDeferredError as error:
            output = {"ok": False, "status": "deferred", "error": str(error)}
        except (ToolError, OSError, ValueError, subprocess.SubprocessError) as error:
            # 操作系统异常可能包含敏感路径；只回传已控制的业务提示。
            output = {
                "ok": False,
                "error": str(error)
                if isinstance(error, ToolError)
                else "工具执行失败：文件编码、访问权限、Git或参数格式无效。",
            }
        if name in self._encoders and "status" in output and output["status"] != "deferred":
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
