"""V5.5：停止写入后备份 JSONL，恢复到新数据目录仍能选择会话并继续。"""

import shutil

from adam.storage import SessionStore


def test_backup_restores_history_in_another_data_directory(tmp_path):
    original = tmp_path / "original"
    with SessionStore(original) as store:
        session_id = store.create_session("迁移演示", "保留约束")
        store.append_turn(session_id, "记住 ORION", "已记住")
        expected = store.load_messages(session_id)
    backup = tmp_path / "backup"
    for path in original.rglob("*.jsonl"):
        target = backup / path.relative_to(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    restored = tmp_path / "restored"
    shutil.copytree(backup, restored)
    with SessionStore(restored) as store:
        session = store.select_session(session_id)
        assert session.title == "迁移演示" and session.system_prompt == "保留约束"
        assert store.load_messages(session_id) == expected
        store.append_turn(session_id, "继续", "新位置可用")
        assert store.load_messages(session_id)[-1].content == "新位置可用"
    with SessionStore(original) as untouched:
        assert untouched.load_messages(session_id) == expected
