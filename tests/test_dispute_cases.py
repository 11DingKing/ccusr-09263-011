"""预约争议案件：登记、双方陈述/证据、权限裁剪、处理决定与关闭后证据管控。

双后端（内存 / SQLite）共同验证：
- 案件关联原预约、双方陈述与处理决定；
- Python 查询按角色裁剪当事方可见材料，无关主体拒绝；
- 案件关闭后仍可查看，但追加未经授权的证据被拒；
- 关闭后仅处理人显式授权（authorized=True）可补证。
"""
from __future__ import annotations

import tempfile
import unittest
from typing import Any

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import CatalogService
from service_09252_008.application.dispute_service import DisputeService
from service_09252_008.application.ports import ManualClock, SequentialIdGenerator, UuidIdGenerator
from service_09252_008.domain.errors import CaseClosedError, NotFoundError, PermissionDeniedError
from service_09252_008.persistence.case_store import InMemoryCaseStore
from service_09252_008.persistence.sqlite_case_store import SQLiteCaseStore
from service_09252_008.persistence.sqlite_store import SQLiteStore
from service_09252_008.persistence.store import InMemoryStore
from tests.helpers import NOW, apply_payload, seed_catalog

AGENT = {"actor_id": "agent_01", "roles": ["support_agent"]}
HANDLER = {"actor_id": "handler_01", "roles": ["case_handler"]}
OUTSIDER = {"actor_id": "stranger", "roles": []}


class _DisputeFixture:
    """构建一组预约 + 争议服务，并返回双方主体标识。"""

    def __init__(self, case_store: Any, booking_store: Any, clock: ManualClock, ids: Any) -> None:
        self.case_store = case_store
        self.booking_store = booking_store
        catalog = CatalogService(booking_store, clock, ids)
        self.bookings = BookingService(booking_store, clock, ids)
        self.disputes = DisputeService(case_store, booking_store, clock, ids)
        self.catalog_ids = seed_catalog(catalog)
        applied = self.bookings.apply(apply_payload(self.catalog_ids, "k-disp-booking"))
        self.booking_id = applied["booking_id"]
        self.applicant = applied["institution"]  # 城北学院
        self.respondent = applied["mentor_id"]

    def applicant_actor(self) -> dict[str, Any]:
        return {"actor_id": self.applicant, "roles": []}

    def respondent_actor(self) -> dict[str, Any]:
        return {"actor_id": self.respondent, "roles": []}

    def close(self) -> None:
        self.case_store.close()
        self.booking_store.close()


def _evidence(party: str, ref: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "party": party,
        "kind": "document",
        "description": f"证据-{ref}",
        "content_ref": ref,
    }
    payload.update(extra)
    return payload


class DisputeCaseTestsMixin:
    case_store: Any
    booking_store: Any

    def _build(self) -> _DisputeFixture:
        raise NotImplementedError

    def _open_case(self, fx: _DisputeFixture) -> str:
        view = fx.disputes.open_case(
            AGENT,
            {"booking_id": fx.booking_id, "title": "升级投诉：课程材料严重不符"},
        )
        self.assertEqual(view["status"], "OPEN")
        self.assertEqual(view["booking_id"], fx.booking_id)
        self.assertEqual(view["applicant_id"], fx.applicant)
        self.assertEqual(view["respondent_id"], fx.respondent)
        return view["case_id"]

    def test_open_case_links_booking_and_default_parties(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            view = fx.disputes.get_case(HANDLER, case_id)
            self.assertEqual(view["opened_by"], "agent_01")
            self.assertIsNone(view["decision"])
        finally:
            fx.close()

    def test_open_case_requires_support_role(self) -> None:
        fx = self._build()
        try:
            with self.assertRaises(PermissionDeniedError):
                fx.disputes.open_case(
                    fx.applicant_actor(), {"booking_id": fx.booking_id, "title": "自行立案"}
                )
        finally:
            fx.close()

    def test_open_case_on_missing_booking_rejected(self) -> None:
        fx = self._build()
        try:
            with self.assertRaises(NotFoundError):
                fx.disputes.open_case(AGENT, {"booking_id": "bkg_nope", "title": "无关联预约"})
        finally:
            fx.close()

    def test_parties_submit_statements_and_evidence(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            fx.disputes.add_statement(
                fx.applicant_actor(), case_id, {"party": "APPLICANT", "content": "到场材料与约定不符"}
            )
            fx.disputes.add_statement(
                fx.respondent_actor(), case_id, {"party": "RESPONDENT", "content": "已按清单备料，批次可追溯"}
            )
            fx.disputes.add_evidence(
                fx.applicant_actor(), case_id, _evidence("APPLICANT", "obj://claim/photo-1", kind="photo")
            )
            fx.disputes.add_evidence(
                fx.respondent_actor(), case_id, _evidence("RESPONDENT", "obj://mentor/batch-log")
            )

            handler_view = fx.disputes.get_case(HANDLER, case_id)
            self.assertEqual({s["party"] for s in handler_view["statements"]}, {"APPLICANT", "RESPONDENT"})
            self.assertEqual({e["party"] for e in handler_view["evidence"]}, {"APPLICANT", "RESPONDENT"})
            self.assertTrue(handler_view["viewer"]["internal"])
        finally:
            fx.close()

    def test_party_cannot_submit_for_counterparty(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            with self.assertRaises(PermissionDeniedError):
                fx.disputes.add_statement(
                    fx.applicant_actor(), case_id, {"party": "RESPONDENT", "content": "冒充对方"}
                )
            with self.assertRaises(PermissionDeniedError):
                fx.disputes.add_evidence(
                    fx.applicant_actor(), case_id, _evidence("RESPONDENT", "obj://forged")
                )
        finally:
            fx.close()

    def test_query_is_clipped_by_permission(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            fx.disputes.add_statement(
                fx.applicant_actor(), case_id, {"party": "APPLICANT", "content": "申请方陈述"}
            )
            fx.disputes.add_statement(
                fx.respondent_actor(), case_id, {"party": "RESPONDENT", "content": "被申请方陈述"}
            )
            fx.disputes.add_evidence(fx.applicant_actor(), case_id, _evidence("APPLICANT", "obj://a"))
            fx.disputes.add_evidence(fx.respondent_actor(), case_id, _evidence("RESPONDENT", "obj://r"))

            # 申请方只见本方陈述/证据，不见对方
            applicant_view = fx.disputes.get_case(fx.applicant_actor(), case_id)
            self.assertEqual(applicant_view["viewer"]["party"], "APPLICANT")
            self.assertEqual([s["content"] for s in applicant_view["statements"]], ["申请方陈述"])
            self.assertEqual([e["content_ref"] for e in applicant_view["evidence"]], ["obj://a"])

            # 被申请方同理
            respondent_view = fx.disputes.get_case(fx.respondent_actor(), case_id)
            self.assertEqual([s["content"] for s in respondent_view["statements"]], ["被申请方陈述"])
            self.assertEqual([e["content_ref"] for e in respondent_view["evidence"]], ["obj://r"])

            # 无关主体不可见
            with self.assertRaises(PermissionDeniedError):
                fx.disputes.get_case(OUTSIDER, case_id)

            # 列表同样裁剪：无关主体看不到该案件
            self.assertEqual(fx.disputes.list_cases(OUTSIDER)["items"], [])
            self.assertEqual(
                fx.disputes.list_cases(fx.applicant_actor())["items"][0]["case_id"], case_id
            )
            self.assertEqual(
                fx.disputes.list_cases(HANDLER, booking_id=fx.booking_id)["items"][0]["case_id"], case_id
            )
        finally:
            fx.close()

    def _decide(self, fx: _DisputeFixture, case_id: str) -> dict[str, Any]:
        return fx.disputes.decide(
            HANDLER,
            case_id,
            {"outcome": "partial", "summary": "部分支持：退还材料差价", "remedy": "refund_material_fee"},
        )

    def test_decision_closes_case_and_is_visible_to_both_parties(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            decided = self._decide(fx, case_id)
            self.assertEqual(decided["status"], "CLOSED")
            self.assertIsNotNone(decided["closed_at"])
            self.assertEqual(decided["decision"]["outcome"], "partial")
            # 决定对当事方公开
            applicant_view = fx.disputes.get_case(fx.applicant_actor(), case_id)
            self.assertEqual(applicant_view["status"], "CLOSED")
            self.assertIsNotNone(applicant_view["decision"])
            self.assertEqual(applicant_view["decision"]["handler_id"], "handler_01")
        finally:
            fx.close()

    def test_only_decider_role_can_close_case(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            with self.assertRaises(PermissionDeniedError):
                fx.disputes.decide(
                    AGENT, case_id, {"outcome": "reject", "summary": "客服无权决定"}
                )
            with self.assertRaises(PermissionDeniedError):
                fx.disputes.decide(
                    fx.applicant_actor(), case_id, {"outcome": "reject", "summary": "当事人无权决定"}
                )
        finally:
            fx.close()

    def test_closed_case_remains_viewable_but_rejects_unauthorized_evidence(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            fx.disputes.add_evidence(fx.applicant_actor(), case_id, _evidence("APPLICANT", "obj://before-1"))
            self._decide(fx, case_id)

            # 关闭后仍可查看（处理人与当事人均可）
            self.assertEqual(fx.disputes.get_case(HANDLER, case_id)["status"], "CLOSED")
            self.assertEqual(fx.disputes.get_case(fx.applicant_actor(), case_id)["status"], "CLOSED")

            # 关闭后当事方追加证据被拒（未经授权）
            with self.assertRaises(CaseClosedError):
                fx.disputes.add_evidence(
                    fx.applicant_actor(), case_id, _evidence("APPLICANT", "obj://after-1")
                )
            # 关闭后即便处理人，未显式授权同样被拒
            with self.assertRaises(CaseClosedError):
                fx.disputes.add_evidence(
                    HANDLER, case_id, _evidence("APPLICANT", "obj://after-2")
                )
            # 关闭后追加陈述一律被拒
            with self.assertRaises(CaseClosedError):
                fx.disputes.add_statement(
                    fx.applicant_actor(), case_id, {"party": "APPLICANT", "content": "关闭后陈述"}
                )
            # 重复决定被拒
            with self.assertRaises(CaseClosedError):
                self._decide(fx, case_id)

            # 被拒的证据没有落库
            refs = {e["content_ref"] for e in fx.disputes.get_case(HANDLER, case_id)["evidence"]}
            self.assertEqual(refs, {"obj://before-1"})
        finally:
            fx.close()

    def test_authorized_evidence_allowed_after_close(self) -> None:
        fx = self._build()
        try:
            case_id = self._open_case(fx)
            self._decide(fx, case_id)
            # 处理人显式授权后可补交，证据带 authorized 标记
            view = fx.disputes.add_evidence(
                HANDLER,
                case_id,
                _evidence("RESPONDENT", "obj://authorized-1", authorized=True),
            )
            added = [e for e in view["evidence"] if e["content_ref"] == "obj://authorized-1"]
            self.assertEqual(len(added), 1)
            self.assertTrue(added[0]["authorized"])

            # 普通当事方即便声称 authorized 也无权授权
            with self.assertRaises(CaseClosedError):
                fx.disputes.add_evidence(
                    fx.applicant_actor(),
                    case_id,
                    _evidence("APPLICANT", "obj://sneaky", authorized=True),
                )
        finally:
            fx.close()


class InMemoryDisputeCaseTests(DisputeCaseTestsMixin, unittest.TestCase):
    def _build(self) -> _DisputeFixture:
        clock = ManualClock(NOW)
        ids = SequentialIdGenerator()
        return _DisputeFixture(InMemoryCaseStore(), InMemoryStore(), clock, ids)


class SQLiteDisputeCaseTests(DisputeCaseTestsMixin, unittest.TestCase):
    def _build(self) -> _DisputeFixture:
        self._tmp = tempfile.TemporaryDirectory()
        clock = ManualClock(NOW)
        ids = UuidIdGenerator()
        booking_store = SQLiteStore(f"{self._tmp.name}/booking.db")
        case_store = SQLiteCaseStore(f"{self._tmp.name}/disputes.db")
        return _DisputeFixture(case_store, booking_store, clock, ids)

    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()
            self._tmp = None

    def test_closed_case_persists_and_still_rejects_evidence_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as db_dir:
            clock = ManualClock(NOW)
            booking_store = SQLiteStore(f"{db_dir}/booking.db")
            case_store = SQLiteCaseStore(f"{db_dir}/disputes.db")
            fx = _DisputeFixture(case_store, booking_store, clock, UuidIdGenerator())
            case_id = self._open_case(fx)
            applicant = fx.applicant
            fx.disputes.add_evidence(
                {"actor_id": applicant, "roles": []}, case_id, _evidence("APPLICANT", "obj://keep")
            )
            self._decide(fx, case_id)
            fx.close()

            # 模拟重启：全新仓储与服务挂载同一数据库（不重新播种目录）
            booking_store2 = SQLiteStore(f"{db_dir}/booking.db")
            case_store2 = SQLiteCaseStore(f"{db_dir}/disputes.db")
            disputes2 = DisputeService(case_store2, booking_store2, ManualClock(NOW), UuidIdGenerator())
            applicant_actor = {"actor_id": applicant, "roles": []}
            view = disputes2.get_case(HANDLER, case_id)
            self.assertEqual(view["status"], "CLOSED")
            self.assertIsNotNone(view["decision"])
            self.assertEqual({e["content_ref"] for e in view["evidence"]}, {"obj://keep"})
            with self.assertRaises(CaseClosedError):
                disputes2.add_evidence(applicant_actor, case_id, _evidence("APPLICANT", "obj://after-restart"))
            case_store2.close()
            booking_store2.close()


if __name__ == "__main__":
    unittest.main()
