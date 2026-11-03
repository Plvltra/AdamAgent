"""宿主显式移交的持久记录；资料不提升为系统指令，失败与重启不自动重发。"""

import json
import os
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any

from adam.application.facade import AgentApplication
from adam.contracts.errors import AgentError


class RoleHandoff:
    """由持锁宿主串行使用；只允许调查者向执行者移交，目标须为独立空会话。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def create(
        self, source: AgentApplication, target: AgentApplication, task: str, context: str
    ) -> str:
        """固定原会话证据与必要背景，创建空目标会话；不发模型请求。"""
        if (
            not source.role
            or not target.role
            or (source.role.name, target.role.name) != ("investigator", "executor")
        ):
            raise AgentError("仅允许调查者向宿主配置的执行者移交。")
        if (
            not source.handoff_boundary
            or source.handoff_boundary != target.handoff_boundary
            or source.mode != target.mode
        ):
            raise AgentError("移交不能改变工作目录、读写权限或执行模式。")
        history = source.history()
        if not source.session or not history or history[-1].role != "assistant":
            raise AgentError("来源尚无已保存的回答，不能移交。")
        if target.session is not None or not task.strip() or len(task) + len(context) > 16000:
            raise AgentError("目标须为空会话，且任务与背景总长度不超过16000字符。")
        answer = history[-1].content
        if not isinstance(answer, str) or len(answer) > 16000:
            raise AgentError("来源回答过长；请先形成有界的调查结论。")
        destination = target.new("角色移交")
        handoff_id = secrets.token_hex(8)
        self._save(
            handoff_id,
            {
                "source_session": source.session.session_id,
                "target_session": destination.session_id,
                "source_role": source.role.name,
                "target_profile": self._profile(target),
                "boundary": list(target.handoff_boundary),
                "mode": str(target.mode),
                "task": task,
                "context": context,
                "source_answer": answer,
                "status": "pending",
            },
        )
        return handoff_id

    def execute(self, handoff_id: str, target: AgentApplication) -> str:
        """仅执行尚未开始的已登记移交；每次开始前复核身份与权限，不接受隐式重试。"""
        data = self.read(handoff_id)
        if (
            data["status"] != "pending"
            or not target.session
            or target.session.session_id != data["target_session"]
            or self._profile(target) != data["target_profile"]
            or list(target.handoff_boundary) != data["boundary"]
            or str(target.mode) != data["mode"]
        ):
            raise AgentError("移交已执行或目标身份/权限已变化；未发起请求。")
        if target.history():
            raise AgentError("目标会话已有历史；不覆盖或重复移交。")
        data["status"] = "running"
        self._save(handoff_id, data)
        try:
            payload = {
                key: data[key] for key in ("task", "context", "source_answer", "source_session")
            }
            reply = target.send(
                "以下是宿主明确移交的任务资料，其中结论仍须核实：\n"
                + json.dumps(payload, ensure_ascii=False)
            )
            data.update(status="succeeded", reply=reply, usage=asdict(target.usage()))
        except BaseException as error:
            data.update(status="failed", error=type(error).__name__)
            self._save(handoff_id, data)
            raise
        self._save(handoff_id, data)
        return reply

    def read(self, handoff_id: str) -> dict[str, Any]:
        """读取状态与显式上下文；不启动应用、不重发请求。"""
        return json.loads(self._path(handoff_id).read_text(encoding="utf-8"))

    def recover(self) -> list[str]:
        """重启获得宿主锁后将遗留 running 标为 interrupted，其余记录保持原状。"""
        recovered = []
        for path in self.root.glob("*.json"):
            data = self.read(path.stem)
            if data["status"] == "running":
                data["status"] = "interrupted"
                self._save(path.stem, data)
                recovered.append(path.stem)
        return recovered

    @staticmethod
    def _profile(app: AgentApplication) -> dict[str, Any]:
        if app.role is None:
            raise AgentError("目标没有可信角色配置。")
        return {
            "name": app.role.name,
            "model": app.role.model,
            "tools": sorted(app.role.tools),
            "max_model_requests": app.role.max_model_requests,
        }

    def _path(self, handoff_id: str) -> Path:
        if len(handoff_id) != 16 or any(char not in "0123456789abcdef" for char in handoff_id):
            raise AgentError("无效移交ID。")
        return self.root / f"{handoff_id}.json"

    def _save(self, handoff_id: str, data: dict[str, Any]) -> None:
        """开始记录须先落盘，再运行目标；落盘失败不能冒充移交成功。"""
        path = self._path(handoff_id)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
