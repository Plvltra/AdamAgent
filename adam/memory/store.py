"""用持锁的原子 JSON 快照保存记忆版本；不读取或改写会话日志。"""

import json
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from adam.contracts.errors import MemoryInputError, MemoryStoreError
from adam.contracts.memory import (
    MemoryRecord,
    MemorySource,
    decode_record,
    timestamp,
    validate_record,
)

MAX_FILE_BYTES = 16 * 1024 * 1024


class MemoryStore:
    """每次操作持锁后重读快照，避免不同进程依赖过期的内存缓存。

    root 是独立记忆目录。构造函数不创建文件；写入先同步临时文件再替换正式文件，
    失败保留旧版本。update/delete 还检查调用方看到的版本，防止静默覆盖并发编辑。
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        lock_timeout: float = 5,
    ) -> None:
        self.root = root
        self.path = root / "memories.json"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock_timeout = lock_timeout

    def list_records(
        self, project: str | None, *, include_global: bool = False
    ) -> list[MemoryRecord]:
        """列出范围内尚未删除的最新版本，包含过期条目；不隐含其他项目。"""
        with self._locked():
            latest = {record.memory_id: record for record in self._load()}
            return [
                record
                for record in latest.values()
                if not record.deleted
                and (record.project == project or (include_global and record.project is None))
            ]

    def history(self, memory_id: str, project: str | None) -> tuple[MemoryRecord, ...]:
        """读取指定范围下的全部版本，包括删除记录；找不到时明确报错。"""
        with self._locked():
            records = [
                record
                for record in self._load()
                if record.memory_id == memory_id and record.project == project
            ]
            if not records:
                raise MemoryInputError("该范围内没有这条记忆。")
            return tuple(records)

    def add(
        self,
        content: str,
        project: str | None,
        source: MemorySource,
        *,
        expires_at: str | None = None,
    ) -> MemoryRecord:
        """添加用户明确提供的事实；返回已落盘版本，失败不创建成功记录。"""
        with self._locked():
            records = self._load()
            existing = {record.memory_id for record in records}
            memory_id = secrets.token_hex(4)
            while memory_id in existing:
                memory_id = secrets.token_hex(4)
            now = self._clock().isoformat()
            record = MemoryRecord(
                memory_id,
                1,
                content.strip(),
                project,
                now,
                now,
                replace(source, operation="add"),
                expires_at,
            )
            self._append(records, record)
            return record

    def update(
        self,
        memory_id: str,
        project: str | None,
        content: str,
        source: MemorySource,
        *,
        expected_revision: int,
        expires_at: str | None = None,
    ) -> MemoryRecord:
        """用明确的新内容及有效期替换目标；expires_at=None 表示取消过期时间。"""
        with self._locked():
            records = self._load()
            previous = self._current(records, memory_id, project, expected_revision)
            record = replace(
                previous,
                revision=previous.revision + 1,
                content=content.strip(),
                updated_at=self._clock().isoformat(),
                expires_at=expires_at,
                source=replace(source, operation="update"),
            )
            self._append(records, record)
            return record

    def delete(
        self,
        memory_id: str,
        project: str | None,
        source: MemorySource,
        *,
        expected_revision: int,
    ) -> MemoryRecord:
        """追加删除版本；历史来源保留，后续列表与检索排除该条目。"""
        with self._locked():
            records = self._load()
            previous = self._current(records, memory_id, project, expected_revision)
            record = replace(
                previous,
                revision=previous.revision + 1,
                deleted=True,
                updated_at=self._clock().isoformat(),
                source=replace(source, operation="delete"),
            )
            self._append(records, record)
            return record

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """锁覆盖读取、校验和替换；等待有上限，不把占用伪装成空库。"""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with FileLock(self.root / "memories.lock", timeout=self._lock_timeout):
                yield
        except Timeout as error:
            raise MemoryStoreError("记忆库被占用，本次操作未完成。") from error
        except OSError as error:
            raise MemoryStoreError(
                "记忆文件操作失败，请检查路径和权限；未报告保存成功。"
            ) from error

    def _load(self) -> list[MemoryRecord]:
        """读取并验证整个版本链；损坏和未来格式拒绝写回，原文件保留。"""
        try:
            with self.path.open("rb") as file:
                raw = file.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return []
        try:
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("memory file too large")
            value = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "records"}
                or type(value["version"]) is not int
                or value["version"] != 1
                or not isinstance(value["records"], list)
            ):
                raise ValueError("invalid memory file")
            records = []
            latest: dict[str, MemoryRecord] = {}
            for item in value["records"]:
                record = decode_record(item)
                previous = latest.get(record.memory_id)
                if previous is None:
                    if record.revision != 1 or record.source.operation != "add" or record.deleted:
                        raise ValueError("invalid initial version")
                elif (
                    record.revision != previous.revision + 1
                    or previous.deleted
                    or record.created_at != previous.created_at
                    or record.project != previous.project
                    or timestamp(record.updated_at) < timestamp(previous.updated_at)
                    or record.source.operation == "add"
                ):
                    raise ValueError("invalid version chain")
                latest[record.memory_id] = record
                records.append(record)
            return records
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MemoryStoreError("记忆库损坏或格式不支持，原文件保留。") from None

    def _append(self, records: list[MemoryRecord], record: MemoryRecord) -> None:
        """先验证候选，再写独立临时文件；正式文件替换前没有可见的新版本。"""
        try:
            validate_record(record)
            previous = next(
                (item for item in reversed(records) if item.memory_id == record.memory_id), None
            )
            if previous and timestamp(record.updated_at) < timestamp(previous.updated_at):
                raise ValueError("clock moved backwards")
        except (ValueError, TypeError, AttributeError):
            raise MemoryInputError(
                "记忆内容、范围、来源或时间无效；内容须为 1～4000 字符。"
            ) from None
        payload = json.dumps(
            {"version": 1, "records": [item.to_dict() for item in [*records, record]]},
            ensure_ascii=False,
        ).encode("utf-8")
        if len(payload) > MAX_FILE_BYTES:
            raise MemoryInputError("记忆库超过 16 MiB，本次未保存；请先备份并整理。")
        temporary = self.root / (secrets.token_hex(16) + ".tmp")
        try:
            with temporary.open("xb") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # 清理失败不能遮蔽保存失败，也不能把已替换的文件报告为未保存。

    @staticmethod
    def _current(
        records: list[MemoryRecord], memory_id: str, project: str | None, expected_revision: int
    ) -> MemoryRecord:
        matches = [
            record
            for record in records
            if record.memory_id == memory_id and record.project == project
        ]
        if not matches or matches[-1].deleted:
            raise MemoryInputError("该范围内没有可修改的记忆。")
        record = matches[-1]
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise MemoryInputError("记忆已被其他操作更新，请重新查看后再修改。")
        return record
