"""争议案件 HTTP 边界：主体请求头、按权限裁剪、关闭后补证的 403/409 映射。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from service_09252_008.application.dispute_service import DisputeService
from service_09252_008.application.ports import SequentialIdGenerator
from service_09252_008.interfaces.http_api import create_server
from service_09252_008.persistence.case_store import InMemoryCaseStore
from tests.helpers import SLOT_END, SLOT_START, make_services, seed_catalog


class DisputeHttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog, bookings, clock, store = make_services()
        cls.ids = seed_catalog(catalog)
        cls.disputes = DisputeService(InMemoryCaseStore(), store, clock, SequentialIdGenerator())
        cls.server = create_server("127.0.0.1", 0, catalog, bookings, cls.disputes)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers or {}
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _actor(self, actor_id: str, *roles: str) -> dict[str, str]:
        return {"X-Actor-Id": actor_id, "X-Actor-Roles": ",".join(roles)}

    def test_dispute_case_flow_over_http(self) -> None:
        apply_body = {
            "idempotency_key": "http-disp-apply",
            "institution": "Hexi University",
            "package_id": self.ids["package_id"],
            "mentor_id": self.ids["mentor_id"],
            "resource_id": self.ids["resource_id"],
            "window_id": self.ids["window_id"],
            "seats": 4,
            "slot_start": SLOT_START,
            "slot_end": SLOT_END,
        }
        status, applied = self._request("POST", "/bookings", apply_body)
        self.assertEqual(status, 201)
        booking_id = applied["booking_id"]
        applicant = applied["institution"]
        respondent = applied["mentor_id"]

        # 非客服立案 -> 403
        status, denied = self._request(
            "POST",
            "/disputes",
            {"booking_id": booking_id, "title": "自行立案"},
            headers=self._actor(applicant),
        )
        self.assertEqual((status, denied["error"]), (403, "permission_denied"))

        # 客服登记升级投诉 -> 立案，默认双方取预约院校与导师
        status, case = self._request(
            "POST",
            "/disputes",
            {"booking_id": booking_id, "title": "升级投诉：材料与约定不符"},
            headers=self._actor("agent_01", "support_agent"),
        )
        self.assertEqual(status, 200)
        case_id = case["case_id"]
        self.assertEqual(case["applicant_id"], applicant)
        self.assertEqual(case["respondent_id"], respondent)

        # 申请方提交陈述与证据
        status, _ = self._request(
            "POST",
            f"/disputes/{case_id}/statements",
            {"party": "APPLICANT", "content": "到场染料与样品不符"},
            headers=self._actor(applicant),
        )
        self.assertEqual(status, 200)
        status, _ = self._request(
            "POST",
            f"/disputes/{case_id}/evidence",
            {"party": "APPLICANT", "kind": "photo", "description": "现场照片", "content_ref": "obj://p1"},
            headers=self._actor(applicant),
        )
        self.assertEqual(status, 200)

        # 申请方视图只见本方材料，看不到被申请方
        status, applicant_view = self._request("GET", f"/disputes/{case_id}", headers=self._actor(applicant))
        self.assertEqual(status, 200)
        self.assertEqual(applicant_view["counts"]["statements_visible"], 1)
        self.assertEqual(applicant_view["counts"]["evidence_visible"], 1)

        # 处理人落决定 -> 关闭
        status, decided = self._request(
            "POST",
            f"/disputes/{case_id}/decision",
            {"outcome": "partial", "summary": "部分支持", "remedy": "refund"},
            headers=self._actor("handler_01", "case_handler"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(decided["status"], "CLOSED")

        # 关闭后仍可查看
        status, _ = self._request("GET", f"/disputes/{case_id}", headers=self._actor(respondent))
        self.assertEqual(status, 200)
        # 无关主体 -> 403
        status, forbidden = self._request("GET", f"/disputes/{case_id}", headers=self._actor("stranger"))
        self.assertEqual((status, forbidden["error"]), (403, "permission_denied"))

        # 关闭后申请方补证（未经授权）-> 409 case_closed
        status, closed_err = self._request(
            "POST",
            f"/disputes/{case_id}/evidence",
            {"party": "APPLICANT", "kind": "document", "description": "迟到材料", "content_ref": "obj://late"},
            headers=self._actor(applicant),
        )
        self.assertEqual((status, closed_err["error"]), (409, "case_closed"))

        # 处理人显式授权补交 -> 200，证据带 authorized
        status, authorized = self._request(
            "POST",
            f"/disputes/{case_id}/evidence",
            {
                "party": "RESPONDENT",
                "kind": "document",
                "description": "批次追溯单",
                "content_ref": "obj://trace",
                "authorized": True,
            },
            headers=self._actor("handler_01", "case_handler"),
        )
        self.assertEqual(status, 200)
        trace = [e for e in authorized["evidence"] if e["content_ref"] == "obj://trace"]
        self.assertEqual(len(trace), 1)
        self.assertTrue(trace[0]["authorized"])

        # 列表支持按预约过滤
        status, listing = self._request(
            "GET", f"/disputes?booking_id={booking_id}", headers=self._actor("handler_01", "case_handler")
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["case_id"] for item in listing["items"]], [case_id])


if __name__ == "__main__":
    unittest.main()
