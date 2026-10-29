"""把本地 Skill 目录接入已有工具；只读指导不改变命令和编辑审批。"""

from pathlib import Path

from adam.config.settings import PermissionConfig
from adam.skills.catalog import SkillCatalog
from adam.tool_calls.policy import PathPolicy
from adam.tool_calls.registry import ToolRegistry

SPECS = {
    "load_skill": {
        "description": "当前任务匹配目录用途或用户点名时，按需加载 Skill 正文或已列出的材料；不匹配则不用。",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "resource": {
                    "type": "string",
                    "description": "默认 SKILL.md，其他材料必须在返回清单中",
                },
            },
            "required": ["skill_id"],
            "additionalProperties": False,
        },
    }
}


def register_skills(
    registry: ToolRegistry,
    workspace: Path,
    permissions: PermissionConfig,
    *,
    enabled: bool = True,
    protected_dirs: tuple[Path, ...] = (),
) -> tuple[ToolRegistry, tuple[dict[str, str], ...]]:
    """返回扩展目录及冻结摘要；关闭或没有 Skill 时不增加工具和上下文。"""
    if not enabled:
        return registry, ()
    catalog = SkillCatalog(
        workspace,
        PathPolicy(workspace, permissions, protected_dirs=protected_dirs).resolve,
        max_bytes=registry.limits.max_text_file_bytes,
        max_result_chars=registry.limits.max_result_chars,
    )
    summaries = catalog.summaries()
    if not summaries:
        return registry, ()
    return registry.extended(
        SPECS,
        {
            "load_skill": lambda args, _: catalog.load(
                args["skill_id"], args.get("resource", "SKILL.md")
            ),
        },
    ), summaries
