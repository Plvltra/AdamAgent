"""识别记忆所属项目，不通过远程仓库地址共享不同工作副本的约定。"""

import os
from pathlib import Path

from adam.contracts.errors import MemoryStoreError


def project_identity(workspace: Path) -> str:
    """返回最近 Git 根目录的规范路径；无 Git 时用工作目录。

    .git 文件同样标记工作副本根。目录移动后视为另一项目，不自动猜测迁移。
    权限或 IO 错误直接报告，不能静默选择更高层目录而扩大记忆范围。
    """
    try:
        directory = workspace.resolve(strict=True)
        if not directory.is_dir():
            raise OSError("workspace is not a directory")
        for parent in (directory, *directory.parents):
            try:
                (parent / ".git").stat()
            except FileNotFoundError:
                continue
            return os.path.normcase(str(parent))
        return os.path.normcase(str(directory))
    except OSError as error:
        raise MemoryStoreError("无法确定记忆所属项目，请检查工作目录和权限。") from error
