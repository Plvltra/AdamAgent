"""同组探索的一次性成功选择；不负责启动线程或重复结算 token。"""

import json
import os
import secrets
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from adam.contracts.errors import StorageError
from adam.contracts.verification import TaskVerifier


class ExplorationGroup:
    """由持有主会话锁的宿主创建；成员是已派发的 (agent_id, run_id)。

    同一实例串行核验并原子保存，保存成功后才发布 winner。重开已有组保留结果，
    不启动任务；跨进程互斥沿用主会话锁。submit 只能传宿主收集的实际子回复。
    """

    def __init__(
        self, path: Path, members: tuple[tuple[str, str], ...], verifier: TaskVerifier
    ) -> None:
        if not members or len(set(members)) != len(members):
            raise ValueError("group requires distinct members")
        self.path, self.verifier = path, verifier
        self._lock = RLock()
        self._state: dict[str, Any] = {
            "version": 1,
            "members": [list(member) for member in members],
            "winner": None,
            "checks": [],
        }
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(saved, dict)
                    or set(saved) != set(self._state)
                    or saved["version"] != 1
                    or saved["members"] != self._state["members"]
                    or not isinstance(saved["checks"], list)
                    or (saved["winner"] is not None and saved["winner"] not in saved["members"])
                ):
                    raise ValueError("invalid exploration snapshot")
                self._state = saved
            else:
                self._save(self._state)
        except (OSError, ValueError) as error:
            raise StorageError("探索任务组无法读取；未重发任务。") from error

    def snapshot(self) -> dict[str, Any]:
        """返回独立副本；winner 表示当时通过验收，不保证文件以后不会变化。"""
        with self._lock:
            return json.loads(json.dumps(self._state))

    def submit(self, agent_id: str, run_id: str, reply: str) -> dict[str, Any]:
        """独立检查一个真实已完成结果；失败允许其他成员继续竞争，已有 winner 不被替换。

        一个运行只核验一次；重复收集直接返回快照。保存故障向上传播，内存不提交。
        """
        with self._lock:
            member = [agent_id, run_id]
            if member not in self._state["members"]:
                raise ValueError("run does not belong to group")
            if self._state["winner"] is not None or any(
                check["member"] == member for check in self._state["checks"]
            ):
                return self.snapshot()
            result = self.verifier.check(reply)
            changed = self.snapshot()
            changed["checks"].append({"member": member, "verification": asdict(result)})
            if result.status == "passed":
                changed["winner"] = member
            self._save(changed)
            self._state = changed
            return self.snapshot()

    def _save(self, state: dict[str, Any]) -> None:
        """先落盘再替换快照；不把部分写入或替换失败当成 winner 已提交。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise StorageError("探索任务组保存失败。") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # 清理故障不能覆盖原始保存错误。
