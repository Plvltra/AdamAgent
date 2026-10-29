"""合并用户与项目技能目录，保留覆盖来源；具体路径权限由单目录加载器控制。"""

import json
from dataclasses import replace
from typing import Any

from adam.contracts.errors import ToolError
from adam.contracts.skills import SkillListing, SkillSelection, validate_catalog
from adam.skills.catalog import SkillCatalog


class SkillLibrary:
    """冻结两级技能目录；项目同名项优先，覆盖项仅供用户查看，不向模型重复提供。"""

    def __init__(self, project: SkillCatalog, user: SkillCatalog | None = None) -> None:
        self._selected: dict[str, tuple[SkillCatalog, SkillListing]] = {}
        listings: list[SkillListing] = []
        if user is not None and user.root == project.root:
            user = None
        for scope, catalog in (("user", user), ("project", project)):
            if catalog is None:
                continue
            for summary in catalog.summaries():
                item = SkillListing(
                    identifier=summary["id"],
                    name=summary["name"],
                    description=summary["description"],
                    format=summary["format"],
                    version=summary["version"],
                    scope=scope,
                    source=str(catalog.source_path(summary["id"])),
                )
                listings.append(item)
                self._selected[item.identifier] = catalog, item
        self._listings = tuple(
            replace(
                item,
                overridden_by=self._selected[item.identifier][1].source
                if self._selected[item.identifier][1] != item
                else None,
            )
            for item in sorted(listings, key=lambda item: (item.identifier, item.scope))
        )
        try:
            validate_catalog(self.summaries())
        except ValueError as error:
            raise ToolError(
                "合并后的 Skill 目录超过16项或6000字符，请精简技能与用途说明。"
            ) from error

    def listings(self) -> tuple[SkillListing, ...]:
        """返回包含生效项和被覆盖项的只读清单；不读正文、不改变选择。"""
        return self._listings

    def summaries(self) -> tuple[dict[str, str], ...]:
        """只给模型生效目录及来源；全局条目不会与同名项目技能重复注入。"""
        return tuple(
            item.summary()
            for _, item in sorted(self._selected.values(), key=lambda pair: pair[1].identifier)
        )

    def select(self, skill_id: str) -> SkillSelection:
        """读取用户为本轮指定的正文及材料清单；不执行脚本，也不粘贴进持久聊天历史。"""
        try:
            loaded = self.load(skill_id)
        except OSError as error:
            raise ToolError("指定 Skill 无法读取，请检查文件与权限。") from error
        return SkillSelection(
            self._selected[skill_id][1],
            loaded["version"],
            loaded["content"],
            tuple(loaded["resources"]),
        )

    def load(self, skill_id: str, resource: str = "SKILL.md") -> dict[str, Any]:
        """从固定的生效来源加载材料；变更或失败不会静默退回被覆盖版本。"""
        if skill_id not in self._selected:
            raise ToolError("Skill 未登记；请先查看可用目录。")
        catalog, item = self._selected[skill_id]
        result = {**catalog.load(skill_id, resource), "scope": item.scope, "source": item.source}
        if len(json.dumps({"ok": True, **result}, ensure_ascii=False)) > catalog.max_result_chars:
            raise ToolError("包含来源的 Skill 结果超过工具上限，请精简材料。")
        return result
