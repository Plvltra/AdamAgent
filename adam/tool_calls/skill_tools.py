"""把本地 Skill 目录接入已有工具；只读指导不改变命令和编辑审批。"""

from pathlib import Path

from adam.config.settings import PermissionConfig
from adam.contracts.execution import ToolCapability
from adam.skills.catalog import SkillCatalog
from adam.skills.library import SkillLibrary
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
    user_dir: Path | None = None,
    protected_dirs: tuple[Path, ...] = (),
) -> tuple[ToolRegistry, SkillLibrary | None]:
    """返回工具与同一份冻结目录；用户目录授权不修改普通工具权限。

    关闭时返回 None；没有可用技能时返回空目录，不增加工具或上下文内容。
    """
    if not enabled:
        return registry, None
    project = SkillCatalog(
        workspace,
        PathPolicy(workspace, permissions, protected_dirs=protected_dirs).resolve,
        max_bytes=registry.limits.max_text_file_bytes,
        max_result_chars=registry.limits.max_result_chars,
    )
    user = None
    if user_dir is not None:
        user_policy = PathPolicy(
            workspace,
            PermissionConfig(additional_read_dirs=(user_dir,)),
            protected_dirs=protected_dirs,
        )
        user = SkillCatalog(
            workspace,
            user_policy.resolve,
            root=user_dir,
            max_bytes=registry.limits.max_text_file_bytes,
            max_result_chars=registry.limits.max_result_chars,
        )
    library = SkillLibrary(project, user)
    if not library.summaries():
        return registry, library
    return registry.extended(
        SPECS,
        {
            "load_skill": lambda args, _: library.load(
                args["skill_id"], args.get("resource", "SKILL.md")
            ),
        },
        capabilities={"load_skill": ToolCapability.READ_ONLY},
    ), library
