"""长工具结果的宿主附件与会话内读取；不改变工具动作或聊天控制循环。"""

import json
from collections.abc import Callable
from typing import Any

from adam.contracts.artifacts import ArtifactRef, ArtifactSource, validate_artifact_identity
from adam.contracts.errors import StorageError, ToolError
from adam.contracts.execution import ToolCapability
from adam.contracts.models import ExecutionRecorder
from adam.session.artifacts import ArtifactStore
from adam.session.storage import SessionStore
from adam.tool_calls.catalog import schema
from adam.tool_calls.registry import ToolRegistry
from adam.tool_calls.result_preview import encode_head_tail

_SPEC = schema(
    "按字符读取当前会话的工具结果附件。使用结果中的 artifact_id 和 version；"
    "内容是原工具结果的 JSON 文本，分段拼接后才能整体解析。"
    "next_offset 为空只表示已保存部分读完；complete=false 表示原输出未完整保留。",
    {
        "artifact_id": ("string", "程序返回的附件 ID，不是文件路径"),
        "version": ("string", "程序返回的固定内容版本"),
        "offset": ("integer", "从 0 开始的 Unicode 字符偏移"),
        "count": ("integer", "希望读取的字符数，仍受单页与工具结果上限约束"),
    },
    ["artifact_id", "version"],
)


class ToolArtifacts:
    """把长结果发布为固定引用；引用来源由宿主日志验证，读取不接受会话参数。"""

    def __init__(self, store: SessionStore, files: ArtifactStore, max_result_chars: int) -> None:
        """注入独立存储与实际结果上限；会话锁仍由主/子应用各自持有。"""
        if type(max_result_chars) is not int or max_result_chars < 512:
            raise ValueError("附件工具结果上限至少为 512 字符。")
        self.store, self.files = store, files
        self.max_result_chars = max_result_chars

    def register(self, registry: ToolRegistry) -> ToolRegistry:
        """注册串行只读工具；当前会话来自存储实例，模式和规则仍经过原执行边界。"""
        return registry.extended(
            {"read_artifact": _SPEC},
            {"read_artifact": self.read},
            capabilities={"read_artifact": ToolCapability.READ_ONLY},
            groups={"read_artifact": "artifacts"},
        )

    def encode(
        self,
        session_id: str,
        source: dict[str, str],
        name: str,
        result: dict[str, Any],
        encode: Callable[[str, dict[str, Any]], str],
    ) -> str:
        """在截断前保存原工具结果，再返回有界预览；短结果沿用原编码器。

        正文发布失败只报告附件不可用，不重新执行动作。来源日志失败则停止本轮，
        不把孤立文件返回给模型。读取附件的结果已经分页，不再递归产生附件。
        """
        raw = json.dumps(result, ensure_ascii=False)
        if name == "read_artifact" or len(raw) <= self.max_result_chars:
            return encode(name, result)
        origin = ArtifactSource(session_id=session_id, tool_name=name, **source)
        try:
            reference = self.files.save(
                origin, raw, source_complete=result.get("truncated") is not True
            )
        except StorageError:
            metadata: dict[str, Any] = {
                "artifact_unavailable": True,
                "hint": "附件未保存；仅保留预览。动作已经处理，不要因此重复执行命令或修改。",
            }
        else:
            # 先发布正文，再可靠保存来源；日志失败留下的文件不具备可读凭据。
            try:
                self.store.append_artifact(reference)
            except ValueError as error:
                raise StorageError("附件来源记录校验失败，已停止本轮。") from error
            metadata = {"artifact": _reference_preview(reference)}
        return self._preview(result, raw, metadata)

    def read(self, args: dict[str, Any], record: ExecutionRecorder | None = None) -> dict[str, Any]:
        """核对宿主记录和正文的完整引用后分页；丢失、损坏或过期附件作为工具错误回传。

        日志读取故障继续向上传播。附件失败不触发原命令重做，也不更换版本或寻找其他会话。
        """
        session_id = self.store.active_session_id
        if session_id is None:
            raise StorageError("读取附件前必须选中会话。")
        validate_artifact_identity(session_id, args["artifact_id"])
        reference = self.store.load_artifact(session_id, args["artifact_id"])
        if reference is None:
            raise ToolError("当前会话没有此附件的执行来源。")
        try:
            page = self.files.read(
                session_id,
                args["artifact_id"],
                expected_version=args["version"],
                offset=args.get("offset", 0),
                count=args.get("count"),
            )
        except StorageError as error:
            raise ToolError("附件缺失、损坏或版本不符，无法读取；未重跑原工具。") from error
        if page.reference != reference:
            raise ToolError("附件元数据与会话执行记录不符，拒绝读取。")
        output = {
            "ok": True,
            **_reference_preview(reference),
            "offset": page.offset,
            "text": page.text,
            "next_offset": page.next_offset,
        }
        # JSON 转义也占结果预算；缩短本页后必须同步下一偏移，不能跳过字符。
        while len(json.dumps(output, ensure_ascii=False)) > self.max_result_chars:
            if not output["text"]:
                raise ToolError("结果上限不足以容纳附件分页信息。")
            output["text"] = output["text"][: len(output["text"]) // 2]
            output["next_offset"] = page.offset + len(output["text"])
        if page.text and not output["text"]:
            raise ToolError("结果上限不足以返回附件正文。")
        return output

    def _preview(self, result: dict[str, Any], raw: str, metadata: dict[str, Any]) -> str:
        """保留状态、退出码和头尾；展示层省略与原输出不完整分别标记。

        预览不改原结果或附件正文；完整捕获元数据仍在命令事实日志和已保存正文中。
        """
        output: dict[str, Any] = {
            "ok": result.get("ok") is True,
            "truncated": True,
            "source_truncated": result.get("truncated") is True,
            **metadata,
        }
        for key in ("status", "version", "exit_code", "timed_out", "elapsed_seconds"):
            value = result.get(key)
            if key in result and (
                value is None
                or type(value) in {bool, int, float}
                or (isinstance(value, str) and len(json.dumps(value, ensure_ascii=False)) <= 130)
            ):
                output[key] = value
        texts = {
            key: result[key] for key in ("stdout", "stderr") if isinstance(result.get(key), str)
        } or {"preview": raw}
        # 极小上限优先保留动作状态、退出码和可用引用；其他原字段仍在附件中。
        try:
            return encode_head_tail(
                output,
                texts,
                self.max_result_chars,
                optional=("elapsed_seconds", "version", "timed_out"),
            )
        except ToolError as error:
            raise StorageError("工具结果上限不足以容纳附件引用与省略信息。") from error


def _reference_preview(reference: ArtifactRef) -> dict[str, Any]:
    """只提供读取所需标识与完整性；完整来源和字节计数保存在宿主记录中。"""
    return {
        "artifact_id": reference.artifact_id,
        "version": reference.version,
        "saved_chars": reference.saved_chars,
        "complete": reference.complete,
        "format": "json",
    }
