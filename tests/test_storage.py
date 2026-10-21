import sqlite3

import pytest

from adam.storage import Session, SessionNotFoundError, SessionStore


def test_create_empty_session(tmp_path):
    with SessionStore(tmp_path / "chat.db") as store:
        assert store.list_sessions() == []
        session_id = store.create_session("学习", "请使用中文")
        expected = Session(session_id=session_id, title="学习", system_prompt="请使用中文")
        assert store.get_session(session_id) == expected
        assert store.list_sessions() == [expected]
        assert store.load_messages(session_id) == []


def test_messages_survive_reopen_in_order(tmp_path):
    path = tmp_path / "chat.db"
    user_text = "引号 ' 和 SQL 文本：'); DROP TABLE sessions; --\n第二行"
    with SessionStore(path) as store:
        session_id = store.create_session("学习", "系统提示")
        store.append_turn(session_id, user_text, "回答一")
        store.append_turn(session_id, "问题二", "回答二")

    with SessionStore(path) as reopened:
        assert reopened.get_session(session_id).system_prompt == "系统提示"
        messages = reopened.load_messages(session_id)
        assert [(m.position, m.role, m.content) for m in messages] == [
            (0, "user", user_text),
            (1, "assistant", "回答一"),
            (2, "user", "问题二"),
            (3, "assistant", "回答二"),
        ]
        assert len({m.message_id for m in messages}) == 4


def test_sessions_are_isolated(tmp_path):
    with SessionStore(tmp_path / "chat.db") as store:
        first = store.create_session("同名", "")
        second = store.create_session("同名", "")
        store.append_turn(first, "A", "A回复")
        store.append_turn(second, "B", "B回复")
        store.append_turn(first, "A2", "A2回复")
        assert [m.content for m in store.load_messages(first)] == ["A", "A回复", "A2", "A2回复"]
        assert [m.content for m in store.load_messages(second)] == ["B", "B回复"]
        assert [m.position for m in store.load_messages(second)] == [0, 1]


def test_second_insert_failure_rolls_back_entire_turn(tmp_path):
    path = tmp_path / "chat.db"
    with SessionStore(path) as store:
        session_id = store.create_session("事务练习", "")
        store.append_turn(session_id, "旧问题", "旧回答")
        original = store.load_messages(session_id)

        # 真实数据库在第二次 INSERT 时失败，验证第一条也不会留下。
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TRIGGER reject_assistant BEFORE INSERT ON messages
                WHEN NEW.role = 'assistant'
                BEGIN
                    SELECT RAISE(ABORT, 'injected assistant insert failure');
                END
                """
            )
            connection.commit()
        finally:
            connection.close()

        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            store.append_turn(session_id, "不能留下", "失败回答")
        assert store.load_messages(session_id) == original

    with SessionStore(path) as reopened:
        assert reopened.load_messages(session_id) == original

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER reject_assistant")
        connection.commit()
    finally:
        connection.close()
    with SessionStore(path) as reopened:
        reopened.append_turn(session_id, "重试问题", "重试回答")
        assert [m.position for m in reopened.load_messages(session_id)] == [0, 1, 2, 3]


def test_missing_session_is_not_an_empty_session(tmp_path):
    with SessionStore(tmp_path / "chat.db") as store:
        with pytest.raises(SessionNotFoundError):
            store.get_session(42)
        with pytest.raises(SessionNotFoundError):
            store.load_messages(42)
        with pytest.raises(SessionNotFoundError):
            store.append_turn(42, "问题", "回答")
        # 失败后连接仍可使用，不残留进行中的事务。
        session_id = store.create_session("正常", "")
        store.append_turn(session_id, "问题", "回答")
        assert len(store.load_messages(session_id)) == 2


def test_reads_and_extends_legacy_database(tmp_path):
    # 独立建库，验证已有文件的读取与追加。
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                session_id INTEGER PRIMARY KEY, title TEXT NOT NULL, system_prompt TEXT NOT NULL
            );
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(session_id),
                position INTEGER NOT NULL CHECK(position >= 0),
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL, UNIQUE(session_id, position)
            );
            INSERT INTO sessions VALUES (7, 'old title', 'old prompt');
            INSERT INTO messages VALUES (11, 7, 0, 'user', 'old question');
            INSERT INTO messages VALUES (12, 7, 1, 'assistant', 'old answer');
        """)
    connection.close()
    with SessionStore(path) as store:
        assert store.get_session(7).system_prompt == "old prompt"
        store.append_turn(7, "new question", "new answer")
        assert [(m.position, m.content) for m in store.load_messages(7)] == [
            (0, "old question"),
            (1, "old answer"),
            (2, "new question"),
            (3, "new answer"),
        ]
        assert store.create_session("new", "") > 7


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": 999},
        {"position": -1},
        {"role": "invalid"},
        {"position": 0},
    ],
)
def test_database_constraints_on_new_connections(tmp_path, change):
    path = tmp_path / "constraints.db"
    with SessionStore(path) as store:
        session_id = store.create_session("constraints", "")
        store.append_turn(session_id, "question", "answer")
    # 关闭重开，验证每个存储连接都会启用外键。
    with SessionStore(path) as store:
        values = dict(session_id=session_id, position=2, role="user", content="invalid")
        values.update(change)
        with pytest.raises(sqlite3.IntegrityError), store._connection:
            store._connection.execute(
                "INSERT INTO messages (session_id, position, role, content) "
                "VALUES (:session_id, :position, :role, :content)",
                values,
            )
        assert len(store.load_messages(session_id)) == 2


def test_two_stores_append_without_duplicate_positions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "concurrent.db"
    with SessionStore(path) as store:
        session_id = store.create_session("concurrent", "")

    def append(number):
        with SessionStore(path) as store:
            store.append_turn(session_id, f"question {number}", f"answer {number}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(append, range(6)))
    with SessionStore(path) as store:
        messages = store.load_messages(session_id)
        assert [m.position for m in messages] == list(range(12))
        for user, assistant in zip(messages[::2], messages[1::2], strict=True):
            assert user.content.split()[-1] == assistant.content.split()[-1]
