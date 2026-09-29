"""SQLite 存储实现：单连接 + 可重入事务，供服务重启后恢复状态。

大部分集合落在通用 ``records`` 文档表；预约争议案件按治理要求物理拆表：
案件、双方陈述、案件证据、处理决定各自独立建表，证据与决定不与案件共表，
关闭案件后证据行不可变（由应用层在事务内判定）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL,
    key        TEXT NOT NULL,
    data       TEXT NOT NULL,
    PRIMARY KEY (collection, key)
);

-- 预约争议案件：案件主表
CREATE TABLE IF NOT EXISTS dispute_cases (
    case_id TEXT PRIMARY KEY,
    status  TEXT NOT NULL,
    data    TEXT NOT NULL
);

-- 双方陈述（与案件拆表）
CREATE TABLE IF NOT EXISTS dispute_statements (
    statement_id TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispute_statements_case ON dispute_statements(case_id);

-- 案件证据（与案件、决定拆表；关闭后由应用层冻结追加）
CREATE TABLE IF NOT EXISTS dispute_evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispute_evidence_case ON dispute_evidence(case_id);

-- 处理决定（与案件、证据拆表）
CREATE TABLE IF NOT EXISTS dispute_decisions (
    decision_id TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispute_decisions_case ON dispute_decisions(case_id);
"""

#: 物理拆表集合 -> (表名, 主键列, 可等值过滤的列 -> 表列名)
_PHYSICAL_TABLES: dict[str, tuple[str, str, dict[str, str]]] = {
    "dispute_cases": ("dispute_cases", "case_id", {"status": "status"}),
    "dispute_statements": ("dispute_statements", "statement_id", {"case_id": "case_id"}),
    "dispute_evidence": ("dispute_evidence", "evidence_id", {"case_id": "case_id"}),
    "dispute_decisions": ("dispute_decisions", "decision_id", {"case_id": "case_id"}),
}


class SQLiteStore:
    """以 SQLite 为后端的文档存储。

    - 写事务使用 ``BEGIN IMMEDIATE``，多线程/多进程下串行化写者；
    - ``transaction`` 可重入，仅最外层提交/回滚；
    - 运行数据落在调用方给定的目录（不得写入源码目录）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path != Path(":memory:"):
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # 排他锁贯穿整个事务：同一线程可重入，其他线程阻塞至外层事务
        # 提交/回滚，与 SQLite 单写者语义一致，保证并发锁定串行化。
        self._lock.acquire()
        try:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.rollback()
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._conn.commit()
        finally:
            self._lock.release()

    def get(self, collection: str, key: str) -> dict[str, Any] | None:
        physical = _PHYSICAL_TABLES.get(collection)
        with self._lock:
            if physical is not None:
                table, key_col, _ = physical
                row = self._conn.execute(
                    f"SELECT data FROM {table} WHERE {key_col} = ?", (key,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT data FROM records WHERE collection = ? AND key = ?", (collection, key)
                ).fetchone()
        return json.loads(row["data"]) if row else None

    def put(self, collection: str, key: str, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        physical = _PHYSICAL_TABLES.get(collection)
        with self._lock:
            if physical is not None:
                table, key_col, filter_cols = physical
                extra_cols = list(filter_cols.values())
                columns = [key_col, *extra_cols, "data"]
                placeholders = ", ".join("?" for _ in columns)
                extra_values = [record.get(field) for field in filter_cols]
                updates = ", ".join(f"{col} = excluded.{col}" for col in [*extra_cols, "data"])
                self._conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT({key_col}) DO UPDATE SET {updates}",
                    (key, *extra_values, payload),
                )
            else:
                self._conn.execute(
                    "INSERT INTO records (collection, key, data) VALUES (?, ?, ?) "
                    "ON CONFLICT(collection, key) DO UPDATE SET data = excluded.data",
                    (collection, key, payload),
                )

    def delete(self, collection: str, key: str) -> None:
        physical = _PHYSICAL_TABLES.get(collection)
        with self._lock:
            if physical is not None:
                table, key_col, _ = physical
                self._conn.execute(f"DELETE FROM {table} WHERE {key_col} = ?", (key,))
            else:
                self._conn.execute(
                    "DELETE FROM records WHERE collection = ? AND key = ?", (collection, key)
                )

    def query(self, collection: str, **filters: Any) -> list[dict[str, Any]]:
        physical = _PHYSICAL_TABLES.get(collection)
        with self._lock:
            if physical is not None:
                table, _, filter_cols = physical
                clauses: list[str] = []
                values: list[Any] = []
                for field, value in filters.items():
                    column = filter_cols.get(field)
                    if column is None:
                        # 未建索引列退化为 JSON 端过滤
                        continue
                    clauses.append(f"{column} = ?")
                    values.append(value)
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = self._conn.execute(f"SELECT data FROM {table}{where}", values).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM records WHERE collection = ?", (collection,)
                ).fetchall()
        records = [json.loads(row["data"]) for row in rows]
        return [r for r in records if all(r.get(field) == value for field, value in filters.items())]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
