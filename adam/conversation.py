"""组织会话流程；数据库是已完成历史的来源。"""

import sqlite3

from adam.llm import LLMClient
from adam.storage import Session, SessionStore


class TurnSaveError(RuntimeError):
    """模型已回答，但本轮未能保存。"""

    def __init__(self, reply: str) -> None:
        super().__init__("本轮保存失败，未加入历史；未自动重新请求模型。")
        self.reply = reply


class Conversation:
    def __init__(self, store: SessionStore, client: LLMClient, system_prompt: str) -> None:
        self.store = store
        self.client = client
        self.system_prompt = system_prompt
        self.session: Session | None = None
        self.model = client.config.model

    def new(self, title: str = "新对话") -> Session:
        session_id = self.store.create_session(title, self.system_prompt)
        return self.resume(session_id)

    def resume(self, session_id: int) -> Session:
        session = self.store.get_session(session_id)
        self.store.load_messages(session_id)
        self.session = session
        return session

    def send(self, text: str) -> str:
        session = self.session or self.new(text[:40])
        messages = (
            [{"role": "system", "content": session.system_prompt}] if session.system_prompt else []
        )
        messages.extend(
            {"role": message.role, "content": message.content}
            for message in self.store.load_messages(session.session_id)
        )
        messages.append({"role": "user", "content": text})
        reply = self.client.complete(messages, model=self.model)
        try:
            self.store.append_turn(session.session_id, text, reply)
        except sqlite3.Error as error:
            raise TurnSaveError(reply) from error
        return reply
