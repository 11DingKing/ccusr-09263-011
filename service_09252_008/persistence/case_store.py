"""争议案件仓储端口与内存实现。

与预约主存储分离：案件、双方陈述、证据、决定各自独立成“表”，
仓储只负责存取，不做权限判断——按权限裁剪由应用服务在 Python 中完成。
事务约定与 :class:`service_09252_008.persistence.store.InMemoryStore` 一致：
可重入、外层异常整体回滚、返回深拷贝。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Protocol

TABLE_CASES = "dispute_cases"
TABLE_STATEMENTS = "dispute_statements"
TABLE_EVIDENCE = "dispute_evidence"
TABLE_DECISIONS = "dispute_decisions"


class CaseStore(Protocol):
    """争议案件仓储端口。"""

    def transaction(self) -> Iterator[None]:
        ...

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        ...

    def put_case(self, record: dict[str, Any]) -> None:
        ...

    def list_cases(self, *, booking_id: str | None = None) -> list[dict[str, Any]]:
        ...

    def add_statement(self, record: dict[str, Any]) -> None:
        ...

    def list_statements(self, case_id: str) -> list[dict[str, Any]]:
        ...

    def add_evidence(self, record: dict[str, Any]) -> None:
        ...

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        ...

    def get_decision(self, case_id: str) -> dict[str, Any] | None:
        ...

    def put_decision(self, record: dict[str, Any]) -> None:
        ...

    def close(self) -> None:
        ...


class InMemoryCaseStore:
    """进程内案件仓储：四张独立表，快照事务支持回滚。"""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict[str, Any]]] = {
            TABLE_CASES: {},
            TABLE_STATEMENTS: {},
            TABLE_EVIDENCE: {},
            TABLE_DECISIONS: {},
        }
        self._lock = threading.RLock()
        self._depth = 0
        self._snapshot: dict[str, dict[str, dict[str, Any]]] | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._lock.acquire()
        try:
            outermost = self._depth == 0
            if outermost:
                self._snapshot = deepcopy(self._tables)
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    if self._snapshot is not None:
                        self._tables = self._snapshot
                    self._snapshot = None
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._snapshot = None
        finally:
            self._lock.release()

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tables[TABLE_CASES].get(case_id)
            return deepcopy(record) if record is not None else None

    def put_case(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._tables[TABLE_CASES][record["case_id"]] = deepcopy(record)

    def list_cases(self, *, booking_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._tables[TABLE_CASES].values())
        if booking_id is not None:
            records = [r for r in records if r.get("booking_id") == booking_id]
        return deepcopy(records)

    def add_statement(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._tables[TABLE_STATEMENTS][record["statement_id"]] = deepcopy(record)

    def list_statements(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            records = [r for r in self._tables[TABLE_STATEMENTS].values() if r.get("case_id") == case_id]
        return deepcopy(records)

    def add_evidence(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._tables[TABLE_EVIDENCE][record["evidence_id"]] = deepcopy(record)

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            records = [r for r in self._tables[TABLE_EVIDENCE].values() if r.get("case_id") == case_id]
        return deepcopy(records)

    def get_decision(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tables[TABLE_DECISIONS].get(case_id)
            return deepcopy(record) if record is not None else None

    def put_decision(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._tables[TABLE_DECISIONS][record["case_id"]] = deepcopy(record)

    def close(self) -> None:  # pragma: no cover - 对称接口
        pass
