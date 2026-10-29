"""记录本轮发现的规则与模型已见版本；主任务、子任务分别持有实例。"""

from pathlib import Path

from adam.contracts.errors import ToolDeferredError, ToolError
from adam.contracts.rules import RuleSelection
from adam.rules.catalog import RuleCatalog


class RuleTracker:
    """新规则先进入模型输入，再允许工具执行；同批后续调用不能绕过等待。"""

    def __init__(self, catalog: RuleCatalog) -> None:
        self.catalog = catalog
        self._targets: tuple[Path, ...] = ()
        self._acknowledged: RuleSelection | None = None
        self._presented = False
        self._active = False
        self._had_rules = False

    def begin(self) -> None:
        """开始一轮独立检查，下一份快照重新读取根规则。"""
        self.end()
        self._active = True

    def end(self) -> None:
        """清除目标与已见状态；不做文件访问，失败收尾也可安全调用。"""
        self._targets = ()
        self._acknowledged = None
        self._presented = False
        self._active = False
        self._had_rules = False

    def snapshot(self, *, preview: bool = False) -> RuleSelection | None:
        """预览只读根规则；运行中返回已发现路径的完整最新快照。

        曾有规则而后删除时保留空快照，明确告诉模型旧约定已撤销。
        未使用规则的普通项目不增加空消息。
        """
        selected = self.catalog.select(() if preview else self._targets)
        if selected.documents or (not preview and self._had_rules):
            return selected
        return None

    def acknowledge(self, selection: RuleSelection | None) -> None:
        """保存实际请求版本；只在请求成功后调用，不把后续磁盘变化误认成已读。"""
        self._acknowledged = selection
        self._presented = True
        self._had_rules |= bool(selection and selection.documents)

    def check(self, targets: tuple[Path, ...]) -> None:
        """发现并检查目标祖先规则；变化通过普通工具结果通知模型，动作尚未发生。

        项目外的目标不扩展规则根；其操作权限仍由对应文件工具或命令审批检查。
        规则正文只经 RuleCatalog 的路径授权读取，不随命令审批扩大范围。
        """
        if not self._active or not self._presented:
            raise ToolError("项目规则尚未进入模型请求，工具未执行。")
        candidates = list(self._targets)
        for target in targets:
            path = target.expanduser()
            if not path.is_absolute():
                path = self.catalog.workspace / path
            if path.resolve().is_relative_to(self.catalog.workspace) and path not in candidates:
                candidates.append(path)
        # 先完成有界授权读取，再接纳目标；非法参数不能污染后续请求的规则选择。
        selected = self.catalog.select(tuple(candidates))
        self._targets = tuple(candidates)
        latest = selected if selected.documents or self._had_rules else None
        if latest != self._acknowledged:
            raise ToolDeferredError(
                "项目规则已更新，尚未执行工具；下一次请求将提供新规则，请先阅读再调用。"
            )
