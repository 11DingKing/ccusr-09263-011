"""预约争议案件：立案关联原预约、双方陈述、按权限裁剪、关闭后证据冻结。

覆盖内存与 SQLite 双后端；SQLite 后端额外验证案件/陈述/证据/决定物理拆表
以及重启后关闭状态仍然冻结追加证据。
"""
from __future__ import annotations

import tempfile
import unittest

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.dispute_service import DisputeCaseService
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.disputes import Principal
from service_09252_008.domain.errors import NotFoundError, PermissionDeniedError, StateError, ValidationError
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import NOW, apply_payload, make_services, seed_catalog

SUPPORT = Principal("support")
ARBITER = Principal("arbiter")
INSTITUTION = Principal("institution", "城北学院")
OTHER_INSTITUTION = Principal("institution", "别的大学")


class DisputeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.disputes, self.clock, self.store = make_services()
        ids = seed_catalog(self.catalog)
        self.ids = ids
        self.mentor = Principal("mentor", ids["mentor_id"])
        applied = self.bookings.apply(apply_payload(ids, "k-dispute-1"))
        self.booking_id = applied["booking_id"]
        case = self.disputes.open_case(
            {
                "booking_id": self.booking_id,
                "title": "升级投诉：导师未按约定到场",
                "category": "service_upgrade",
            },
            SUPPORT,
        )
        self.case_id = case["case_id"]
        self.assertEqual(case["status"], "OPEN")
        self.assertEqual(case["booking_snapshot"]["booking_id"], self.booking_id)
        self.assertEqual(case["booking_snapshot"]["institution"], "城北学院")

    # ------------------------------------------------------------------
    # 立案
    # ------------------------------------------------------------------

    def test_only_support_can_open_case(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.disputes.open_case(
                {"booking_id": self.booking_id, "title": "x", "category": "c"}, INSTITUTION
            )
        with self.assertRaises(PermissionDeniedError):
            self.disputes.open_case(
                {"booking_id": self.booking_id, "title": "x", "category": "c"}, ARBITER
            )

    def test_open_case_requires_existing_booking(self) -> None:
        with self.assertRaises(NotFoundError):
            self.disputes.open_case(
                {"booking_id": "bkg_missing", "title": "x", "category": "c"}, SUPPORT
            )

    # ------------------------------------------------------------------
    # 陈述与证据
    # ------------------------------------------------------------------

    def test_parties_file_statements(self) -> None:
        self.disputes.add_statement(
            self.case_id, {"content": "导师迟到一小时且未补发材料"}, INSTITUTION
        )
        self.disputes.add_statement(
            self.case_id, {"content": "校方临时更换教室导致延误"}, self.mentor
        )
        view = self.disputes.get_case(self.case_id, SUPPORT)
        parties = {s["party"] for s in view["statements"]}
        self.assertEqual(parties, {"INSTITUTION", "MENTOR"})

    def test_support_records_statement_on_behalf_of_party(self) -> None:
        self.disputes.add_statement(
            self.case_id,
            {"party": "INSTITUTION", "content": "电话投诉记录", "internal": True},
            SUPPORT,
        )
        # 内部笔录对院校不可见
        institution_view = self.disputes.get_case(self.case_id, INSTITUTION)
        self.assertEqual(institution_view["statements"], [])
        support_view = self.disputes.get_case(self.case_id, SUPPORT)
        self.assertEqual(len(support_view["statements"]), 1)
        self.assertTrue(support_view["statements"][0]["internal"])

    def test_party_cannot_file_internal_or_confidential(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.disputes.add_statement(
                self.case_id, {"content": "x", "internal": True}, INSTITUTION
            )
        with self.assertRaises(PermissionDeniedError):
            self.disputes.add_evidence(
                self.case_id,
                {"title": "秘件", "evidence_type": "document", "confidential": True},
                INSTITUTION,
            )

    def test_evidence_permission_trimming(self) -> None:
        self.disputes.add_evidence(
            self.case_id,
            {"title": "现场照片", "evidence_type": "photo"},
            INSTITUTION,
        )
        self.disputes.add_evidence(
            self.case_id,
            {"title": "内部调查笔录", "evidence_type": "document", "confidential": True},
            SUPPORT,
        )
        institution_view = self.disputes.get_case(self.case_id, INSTITUTION)
        titles = {e["title"] for e in institution_view["evidence"]}
        self.assertEqual(titles, {"现场照片"})
        support_view = self.disputes.get_case(self.case_id, SUPPORT)
        self.assertEqual({e["title"] for e in support_view["evidence"]}, {"现场照片", "内部调查笔录"})

    def test_non_participant_cannot_view(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.disputes.get_case(self.case_id, OTHER_INSTITUTION)
        self.assertEqual(self.disputes.list_cases(OTHER_INSTITUTION)["items"], [])
        self.assertEqual(len(self.disputes.list_cases(INSTITUTION)["items"]), 1)

    # ------------------------------------------------------------------
    # 关闭与证据冻结
    # ------------------------------------------------------------------

    def _close_case(self) -> dict:
        self.disputes.add_evidence(
            self.case_id, {"title": "关闭前证据", "evidence_type": "receipt"}, INSTITUTION
        )
        return self.disputes.decide(
            self.case_id,
            {
                "outcome": "FOR_INSTITUTION",
                "summary": "导师确有延误，安排补課并退还部分费用",
                "actions": ["安排一次补課", "退还 30% 费用"],
                "internal_rationale": "导师过往两季度已有两次同类投诉",
            },
            ARBITER,
        )

    def test_only_arbiter_can_decide(self) -> None:
        with self.assertRaises(PermissionDeniedError):
            self.disputes.decide(
                self.case_id, {"outcome": "WITHDRAWN", "summary": "x"}, SUPPORT
            )
        with self.assertRaises(PermissionDeniedError):
            self.disputes.decide(
                self.case_id, {"outcome": "WITHDRAWN", "summary": "x"}, INSTITUTION
            )

    def test_closed_case_viewable_but_evidence_frozen(self) -> None:
        closed = self._close_case()
        self.assertEqual(closed["status"], "CLOSED")
        self.assertIsNotNone(closed["closed_at"])
        self.assertEqual(closed["decision"]["outcome"], "FOR_INSTITUTION")

        # 关闭后仍允许查看
        view = self.disputes.get_case(self.case_id, INSTITUTION)
        self.assertEqual(view["status"], "CLOSED")
        self.assertEqual({e["title"] for e in view["evidence"]}, {"关闭前证据"})
        # 内部理由不向双方公开
        self.assertIsNone(view["decision"]["internal_rationale"])
        # 办案组可见内部理由
        support_view = self.disputes.get_case(self.case_id, SUPPORT)
        self.assertEqual(support_view["decision"]["internal_rationale"], "导师过往两季度已有两次同类投诉")

        # 关闭后追加证据被拒（双方与客服均被拒）
        for principal in (INSTITUTION, self.mentor, SUPPORT):
            with self.subTest(principal=str(principal)):
                with self.assertRaises(StateError) as ctx:
                    self.disputes.add_evidence(
                        self.case_id,
                        {"title": "关闭后新证据", "evidence_type": "photo"},
                        principal,
                    )
                self.assertTrue(ctx.exception.details.get("authorization_required"))

        # 已有证据未被改动，案件视图仍可正常查看
        view_after = self.disputes.get_case(self.case_id, SUPPORT)
        self.assertEqual(len(view_after["evidence"]), 1)

        # 陈述同样冻结，决定不可重复作出
        with self.assertRaises(StateError):
            self.disputes.add_statement(self.case_id, {"content": "补充"}, INSTITUTION)
        with self.assertRaises(StateError):
            self.disputes.decide(
                self.case_id, {"outcome": "WITHDRAWN", "summary": "x"}, ARBITER
            )

    def test_arbiter_authorized_evidence_after_close(self) -> None:
        self._close_case()
        # 仲裁无授权仍被拒
        with self.assertRaises(StateError):
            self.disputes.add_evidence(
                self.case_id, {"title": "无授权补证", "evidence_type": "document"}, ARBITER
            )
        # 双方即便伪造授权字段也被拒（必须仲裁身份）
        with self.assertRaises(PermissionDeniedError):
            self.disputes.add_evidence(
                self.case_id,
                {
                    "title": "伪造授权",
                    "evidence_type": "document",
                    "authorization": {"granted_by": "arbiter", "reason": "复审"},
                },
                INSTITUTION,
            )
        # 仲裁持显式授权补录成功，并留下授权标记
        view = self.disputes.add_evidence(
            self.case_id,
            {
                "title": "授权补录的新证据",
                "evidence_type": "document",
                "authorization": {"granted_by": "arbiter", "reason": "监管复审要求补查"},
            },
            ARBITER,
        )
        self.assertEqual(view["status"], "CLOSED")
        added = next(e for e in view["evidence"] if e["title"] == "授权补录的新证据")
        self.assertTrue(added["authorized_after_close"])
        self.assertEqual(added["authorization"]["granted_by"], "arbiter")
        self.assertEqual(added["authorization"]["reason"], "监管复审要求补查")

    def test_evidence_rejected_when_authorization_invalid(self) -> None:
        self._close_case()
        base = {"title": "补证", "evidence_type": "document"}
        # 授权人不是仲裁 -> 载荷校验失败
        with self.assertRaises(ValidationError):
            self.disputes.add_evidence(
                self.case_id, {**base, "authorization": {"granted_by": "support", "reason": "r"}}, ARBITER
            )
        # 授权理由为空白 -> 载荷校验失败
        with self.assertRaises(ValidationError):
            self.disputes.add_evidence(
                self.case_id, {**base, "authorization": {"granted_by": "arbiter", "reason": "  "}}, ARBITER
            )


class DisputeCaseSQLiteTests(unittest.TestCase):
    """SQLite 物理拆表 + 重启后关闭状态与冻结规则仍然生效。"""

    def test_split_tables_and_freeze_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)

            store = SQLiteStore(db_path)
            catalog, bookings, disputes, _, _ = self._build(store, clock)
            ids = seed_catalog(catalog)
            applied = bookings.apply(apply_payload(ids, "k-sql-dispute"))
            booking_id = applied["booking_id"]
            opened = disputes.open_case(
                {"booking_id": booking_id, "title": "SQL 案件", "category": "upgrade"}, SUPPORT
            )
            case_id = opened["case_id"]
            disputes.add_statement(
                case_id,
                {"party": "INSTITUTION", "content": "院校书面情况说明"},
                SUPPORT,
            )
            disputes.add_evidence(
                case_id, {"title": "停损单", "evidence_type": "receipt"}, Principal("institution", "城北学院")
            )
            disputes.decide(
                case_id,
                {"outcome": "COMPROMISE", "summary": "各承担一半损失"},
                ARBITER,
            )
            # 关闭后追加证据被拒
            with self.assertRaises(StateError):
                disputes.add_evidence(
                    case_id,
                    {"title": "重启前尝试补证", "evidence_type": "photo"},
                    SUPPORT,
                )
            store.close()

            # 证据/决定/陈述/案件确实落在各自的物理表，而非通用 records 表
            import sqlite3

            conn = sqlite3.connect(db_path)
            try:
                for table in ("dispute_cases", "dispute_statements", "dispute_evidence", "dispute_decisions"):
                    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    self.assertGreaterEqual(count, 1, f"table {table} should hold rows")
                leaked = conn.execute(
                    "SELECT COUNT(*) FROM records WHERE collection LIKE 'dispute_%'"
                ).fetchone()[0]
                self.assertEqual(leaked, 0, "dispute collections must not be stored in generic records table")
            finally:
                conn.close()

            # 重启：新服务挂载同一数据库
            clock.advance(hours=2)
            store2 = SQLiteStore(db_path)
            _, _, disputes2, _, _ = self._build(store2, clock)
            view = disputes2.get_case(case_id, SUPPORT)
            self.assertEqual(view["status"], "CLOSED")
            self.assertEqual(view["decision"]["outcome"], "COMPROMISE")
            self.assertEqual({e["title"] for e in view["evidence"]}, {"停损单"})
            # 关闭后追加证据在重启后仍被拒
            with self.assertRaises(StateError):
                disputes2.add_evidence(
                    case_id,
                    {"title": "重启后补证", "evidence_type": "photo"},
                    SUPPORT,
                )
            # 授权补证在重启后仍可用
            authorized = disputes2.add_evidence(
                case_id,
                {
                    "title": "重启后授权补证",
                    "evidence_type": "document",
                    "authorization": {"granted_by": "arbiter", "reason": "司法调阅"},
                },
                ARBITER,
            )
            self.assertEqual(len(authorized["evidence"]), 2)
            store2.close()

    @staticmethod
    def _build(store: SQLiteStore, clock: ManualClock):
        ids = UuidIdGenerator()
        catalog = CatalogService(store, clock, ids)
        bookings = BookingService(store, clock, ids)
        disputes = DisputeCaseService(store, clock, ids)
        return catalog, bookings, disputes, clock, store


if __name__ == "__main__":
    unittest.main()
