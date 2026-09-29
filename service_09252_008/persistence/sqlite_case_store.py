"""争议案件的 SQLite 仓储。

案件、双方陈述、证据、决定分别建表（证据与决定显式拆表，不与案件头
共表）。SQLite 只负责按主键/外键（``case_id``）取行；“某主体能看到
哪些行”的权限裁剪在应用服务的 Python 代码中完成，SQL 层不嵌入角色
规则。

- 写事务使用 ``BEGIN IMMEDIATE``，多线程下写者串行；
- ``transaction`` 可重入，仅最外层提交/回滚；
- 运行数据落在调用方给定目录（不得写入源码目录）。
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
CREATE TABLE IF NOT EXISTS dispute_cases (
    case_id    TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL,
    status     TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispute_cases_booking ON dispute_cases(booking_id);

CREATE TABLE IF NOT EXISTS dispute_statements (
    statement_id TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES dispute_cases(case_id),
    party        TEXT NOT NULL,
    data         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispute_statements_case ON dispute_statements(case_id);

CREATE TABLE IF NOT EXISTS dispute_evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES dispute_cases(case_id),
    party       TEXT NOT NULL,
    authorized  INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispute_evidence_case ON dispute_evidence(case_id);

CREATE TABLE IF NOT EXISTS dispute_decisions (
    case_id     TEXT PRIMARY KEY REFERENCES dispute_cases(case_id),
    decision_id TEXT NOT NULL,
    data        TEXT NOT NULL
);
"""


class SQLiteCaseStore:
    """以独立 SQLite 数据库承载的争议案件仓储。"""

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

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM dispute_cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def put_case(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispute_cases (case_id, booking_id, status, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (case_id) DO UPDATE SET booking_id = excluded.booking_id, "
                "status = excluded.status, data = excluded.data",
                (record["case_id"], record["booking_id"], record["status"], payload),
            )

    def list_cases(self, *, booking_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if booking_id is None:
                rows = self._conn.execute("SELECT data FROM dispute_cases").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM dispute_cases WHERE booking_id = ?", (booking_id,)
                ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def add_statement(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispute_statements (statement_id, case_id, party, data) VALUES (?, ?, ?, ?)",
                (record["statement_id"], record["case_id"], record["party"], payload),
            )

    def list_statements(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM dispute_statements WHERE case_id = ? ORDER BY rowid", (case_id,)
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def add_evidence(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispute_evidence (evidence_id, case_id, party, authorized, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record["evidence_id"],
                    record["case_id"],
                    record["party"],
                    1 if record.get("authorized") else 0,
                    payload,
                ),
            )

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM dispute_evidence WHERE case_id = ? ORDER BY rowid", (case_id,)
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def get_decision(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM dispute_decisions WHERE case_id = ?", (case_id,)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def put_decision(self, record: dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispute_decisions (case_id, decision_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT (case_id) DO UPDATE SET decision_id = excluded.decision_id, data = excluded.data",
                (record["case_id"], record["decision_id"], payload),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
