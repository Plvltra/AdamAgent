"""委派索引的原子快照；完整聊天仍由每个子会话自己的 JSONL 保存。"""

import json
import os
import secrets
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from adam.contracts.delegation import ChildAgent, ChildRun
from adam.contracts.errors import StorageError
from adam.contracts.models import valid_session_id


class DelegationStore:
    """由持有主会话锁的进程使用；线程锁保护索引，子线程不共享 SessionStore。

    索引位于主会话专属目录。先 fsync 临时文件再原子替换，失败保留旧快照；
    子会话的请求、消息和中断恢复仍以 JSONL 为准，不自动重发任务。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "index.json"
        self._lock = RLock()
        self._agents: dict[str, ChildAgent] = {}
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not self.path.exists() and any((root / "sessions").glob("*/*/*/*.jsonl")):
                raise ValueError("missing index for existing child logs")
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("agents"), list):
                    raise ValueError("invalid delegation index")
                for item in data["agents"]:
                    agent = self._decode(item)
                    if agent.agent_id in self._agents:
                        raise ValueError("duplicate child")
                    self._agents[agent.agent_id] = agent
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise StorageError("子 Agent 索引损坏或无法读取；未重发任何任务。") from None

    def agents(self) -> list[ChildAgent]:
        """返回独立快照，调用方不能绕过原子保存修改索引。"""
        with self._lock:
            return deepcopy(list(self._agents.values()))

    def get(self, agent_id: str) -> ChildAgent:
        """读取当前主会话下的子 Agent；其他主会话的 ID 不会被解析为路径。"""
        with self._lock:
            if agent_id not in self._agents:
                raise KeyError(agent_id)
            return deepcopy(self._agents[agent_id])

    def put(self, agent: ChildAgent) -> None:
        """原子保存新子会话或新运行；成功后才替换内存快照。"""
        with self._lock:
            changed = deepcopy(self._agents)
            changed[agent.agent_id] = deepcopy(agent)
            self._save(changed)
            self._agents = changed

    def update_run(self, agent_id: str, run_id: str, **changes: str | None) -> None:
        """更新指定运行，避免主线程查询与子线程收尾覆盖彼此的数据。"""
        with self._lock:
            agent = self.get(agent_id)
            run = next(item for item in agent.runs if item.run_id == run_id)
            for name, value in changes.items():
                if name not in {"status", "child_turn_id", "reply", "error"}:
                    raise ValueError("invalid run update")
                setattr(run, name, value)
            self.put(agent)

    def _save(self, agents: dict[str, ChildAgent]) -> None:
        """索引只保存受控配置与任务内容，不写模型连接配置或 API 密钥。"""
        temporary = self.path.with_suffix("." + secrets.token_hex(8) + ".tmp")
        try:
            value = {"version": 1, "agents": [asdict(agent) for agent in agents.values()]}
            with temporary.open("x", encoding="utf-8", newline="\n") as file:
                json.dump(value, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise StorageError("子 Agent 索引保存失败；未自动重试任务。") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # 清理临时文件失败不能遮蔽最初的保存错误。

    @staticmethod
    def _decode(item: dict[str, Any]) -> ChildAgent:
        """拒绝不支持的索引结构和路径身份；不根据模型参数寻找任意文件。"""
        if set(item) != {"agent_id", "name", "workspace", "read_dirs", "runs"}:
            raise ValueError("invalid agent fields")
        if not isinstance(item["agent_id"], str) or not valid_session_id(item["agent_id"]):
            raise ValueError("invalid child id")
        if not all(isinstance(item[key], str) for key in ("name", "workspace")):
            raise ValueError("invalid child metadata")
        if not isinstance(item["read_dirs"], list) or not all(
            isinstance(value, str) for value in item["read_dirs"]
        ):
            raise ValueError("invalid child scope")
        if not isinstance(item["runs"], list):
            raise ValueError("invalid child runs")
        runs = [ChildRun(**run) for run in item["runs"]]
        if len({run.run_id for run in runs}) != len(runs):
            raise ValueError("duplicate child run")
        for run in runs:
            if run.status not in {"running", "completed", "failed", "interrupted"}:
                raise ValueError("invalid child state")
            if not all(
                isinstance(value, str)
                for value in (
                    run.run_id,
                    run.parent_turn_id,
                    run.request_id,
                    run.tool_call_id,
                    run.task,
                    run.model,
                    run.reply,
                    run.error,
                )
            ):
                raise ValueError("invalid child run")
            if run.child_turn_id is not None and not isinstance(run.child_turn_id, str):
                raise ValueError("invalid child turn")
        return ChildAgent(
            item["agent_id"], item["name"], item["workspace"], item["read_dirs"], runs
        )
