"""预约争议案件应用服务。

用例编排：

- 客服把升级投诉登记为案件（关联原预约）；
- 双方分别提交陈述与证据；
- 处理人落处理决定，一案一决定，落定即关闭；
- 关闭后案件仍可查看，但追加证据必须显式授权（仅处理人可授权补交）。

权限在 Python 查询路径上裁剪：内部角色（客服/处理人/管理员）可见全部
材料；当事方仅可见本方陈述、本方证据与处理决定；无关主体不可见。
所有“读-判-写”都在单个案件仓储事务内完成。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain.disputes import (
    CASE_DECIDE_ROLES,
    CASE_OPEN_ROLES,
    INTERNAL_ROLES,
    Actor,
    CaseDecision,
    CaseEvidence,
    DisputeCase,
    DisputeCaseStatus,
    DisputeParty,
    PartyStatement,
)
from ..domain.errors import (
    CaseClosedError,
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.models import dt_to_str
from ..persistence.case_store import CaseStore
from ..persistence.store import Store
from .booking_service import COLLECTION_BOOKINGS
from .ports import Clock, IdGenerator

EVIDENCE_KINDS = frozenset({"document", "photo", "recording", "other"})
DECISION_OUTCOMES = frozenset({"uphold", "reject", "partial", "mediate"})


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class DisputeService:
    """争议案件用例编排。"""

    def __init__(self, case_store: CaseStore, booking_store: Store, clock: Clock, ids: IdGenerator) -> None:
        self._cases = case_store
        self._bookings = booking_store
        self._clock = clock
        self._ids = ids

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _actor(actor: Actor | dict[str, Any]) -> Actor:
        if isinstance(actor, Actor):
            return actor
        if not isinstance(actor, dict):
            raise ValidationError("actor must be an object with actor_id and roles")
        actor_id = actor.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ValidationError("actor.actor_id must be a non-empty string")
        roles = actor.get("roles", ())
        if not isinstance(roles, (list, tuple, set, frozenset)):
            raise ValidationError("actor.roles must be a list of role names")
        return Actor(actor_id=actor_id.strip(), roles=frozenset(str(r) for r in roles))

    @staticmethod
    def _non_empty(request: dict[str, Any], field: str) -> str:
        value = request.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"field {field} must be a non-empty string", details={"field": field})
        return value.strip()

    def _load_case(self, case_id: str) -> DisputeCase:
        record = self._cases.get_case(case_id)
        if record is None:
            raise NotFoundError(f"dispute case not found: {case_id}", details={"case_id": case_id})
        return DisputeCase.from_dict(record)

    def _save_case(self, case: DisputeCase) -> None:
        case.updated_at = self._clock.now()
        self._cases.put_case(case.to_dict())

    def _require_booking(self, booking_id: str) -> dict[str, Any]:
        record = self._bookings.get(COLLECTION_BOOKINGS, booking_id)
        if record is None:
            raise NotFoundError(f"booking not found: {booking_id}", details={"booking_id": booking_id})
        return record

    def _authorize_party(
        self, actor: Actor, case: DisputeCase, party: DisputeParty
    ) -> str:
        """校验提交主体有权以某当事方身份提交材料，返回实际提交人标识。"""
        if actor.has_any(INTERNAL_ROLES):
            # 内部人员可代任一方登记，提交人记为操作者本人。
            return actor.actor_id
        side = case.party_of(actor.actor_id)
        if side is None:
            raise PermissionDeniedError(
                "actor is not a party to this case",
                details={"case_id": case.case_id, "actor_id": actor.actor_id},
            )
        if side != party:
            raise PermissionDeniedError(
                "actor can only submit materials for its own party",
                details={"case_id": case.case_id, "allowed_party": side.value, "requested_party": party.value},
            )
        return actor.actor_id

    # ------------------------------------------------------------------
    # 登记案件
    # ------------------------------------------------------------------

    def open_case(self, actor: Actor | dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        """客服把升级投诉登记为预约争议案件。"""
        actor = self._actor(actor)
        if not actor.has_any(CASE_OPEN_ROLES):
            raise PermissionDeniedError(
                "actor is not allowed to register dispute cases",
                details={"actor_id": actor.actor_id},
            )
        with self._cases.transaction():
            booking_id = self._non_empty(request, "booking_id")
            booking = self._require_booking(booking_id)
            title = self._non_empty(request, "title")
            applicant_id = request.get("applicant_id")
            respondent_id = request.get("respondent_id")
            # 默认以预约申请院校为申请方、预约导师为被申请方
            applicant = (
                applicant_id.strip()
                if isinstance(applicant_id, str) and applicant_id.strip()
                else booking["institution"]
            )
            respondent = (
                respondent_id.strip()
                if isinstance(respondent_id, str) and respondent_id.strip()
                else booking["mentor_id"]
            )
            if applicant == respondent:
                raise ValidationError("applicant_id and respondent_id must be different parties")
            now = self._clock.now()
            case = DisputeCase(
                case_id=self._ids.new_id("dsp"),
                booking_id=booking_id,
                title=title,
                opened_by=actor.actor_id,
                applicant_id=applicant,
                respondent_id=respondent,
                status=DisputeCaseStatus.OPEN,
                created_at=now,
                updated_at=now,
            )
            self._cases.put_case(case.to_dict())
            return self._case_view(case, actor)

    # ------------------------------------------------------------------
    # 双方陈述
    # ------------------------------------------------------------------

    def add_statement(self, actor: Actor | dict[str, Any], case_id: str, request: dict[str, Any]) -> dict[str, Any]:
        actor = self._actor(actor)
        with self._cases.transaction():
            case = self._load_case(case_id)
            if case.status == DisputeCaseStatus.CLOSED:
                # 关闭后仅可查看；陈述不再接受（证据的授权补交见 add_evidence）。
                raise CaseClosedError(
                    "case is closed; statements can no longer be added",
                    details={"case_id": case_id},
                )
            party = self._parse_party(request)
            submitter = self._authorize_party(actor, case, party)
            content = self._non_empty(request, "content")
            statement = PartyStatement(
                statement_id=self._ids.new_id("stm"),
                case_id=case_id,
                party=party,
                author_id=submitter,
                content=content,
                created_at=self._clock.now(),
            )
            self._cases.add_statement(statement.to_dict())
            self._save_case(case)
            return self._case_view(case, actor)

    # ------------------------------------------------------------------
    # 证据（关闭后追加须授权）
    # ------------------------------------------------------------------

    def add_evidence(self, actor: Actor | dict[str, Any], case_id: str, request: dict[str, Any]) -> dict[str, Any]:
        actor = self._actor(actor)
        with self._cases.transaction():
            case = self._load_case(case_id)
            party = self._parse_party(request)
            submitter = self._authorize_party(actor, case, party)

            kind = request.get("kind", "other")
            if kind not in EVIDENCE_KINDS:
                raise ValidationError(
                    "field kind is not supported",
                    details={"allowed": sorted(EVIDENCE_KINDS)},
                )
            description = self._non_empty(request, "description")
            content_ref = self._non_empty(request, "content_ref")

            authorized_after_close = False
            if case.status == DisputeCaseStatus.CLOSED:
                # 案件关闭后不允许追加未经授权的证据：仅处理人凭显式授权可补交。
                if not actor.has_any(CASE_DECIDE_ROLES) or request.get("authorized") is not True:
                    raise CaseClosedError(
                        "case is closed; adding evidence requires explicit authorization from a handler",
                        details={"case_id": case_id},
                    )
                authorized_after_close = True

            fingerprint = hashlib.sha256(
                _canonical({"kind": kind, "content_ref": content_ref, "description": description}).encode("utf-8")
            ).hexdigest()
            evidence = CaseEvidence(
                evidence_id=self._ids.new_id("evd"),
                case_id=case_id,
                party=party,
                submitter_id=submitter,
                kind=kind,
                description=description,
                content_ref=content_ref,
                content_fingerprint=fingerprint,
                authorized=authorized_after_close,
                created_at=self._clock.now(),
            )
            self._cases.add_evidence(evidence.to_dict())
            self._save_case(case)
            return self._case_view(case, actor)

    # ------------------------------------------------------------------
    # 处理决定（落定即关闭）
    # ------------------------------------------------------------------

    def decide(self, actor: Actor | dict[str, Any], case_id: str, request: dict[str, Any]) -> dict[str, Any]:
        actor = self._actor(actor)
        if not actor.has_any(CASE_DECIDE_ROLES):
            raise PermissionDeniedError(
                "actor is not allowed to issue a case decision",
                details={"actor_id": actor.actor_id},
            )
        with self._cases.transaction():
            case = self._load_case(case_id)
            if case.status == DisputeCaseStatus.CLOSED:
                raise CaseClosedError(
                    "case is already decided and closed",
                    details={"case_id": case_id},
                )
            outcome = request.get("outcome")
            if outcome not in DECISION_OUTCOMES:
                raise ValidationError(
                    "field outcome is not supported",
                    details={"allowed": sorted(DECISION_OUTCOMES)},
                )
            summary = self._non_empty(request, "summary")
            remedy = request.get("remedy")
            if remedy is not None and (not isinstance(remedy, str) or not remedy.strip()):
                raise ValidationError("field remedy must be a non-empty string when provided")
            now = self._clock.now()
            decision = CaseDecision(
                decision_id=self._ids.new_id("dec"),
                case_id=case_id,
                handler_id=actor.actor_id,
                outcome=outcome,
                summary=summary,
                remedy=remedy.strip() if isinstance(remedy, str) else None,
                created_at=now,
            )
            self._cases.put_decision(decision.to_dict())
            case.status = DisputeCaseStatus.CLOSED
            case.closed_at = now
            self._save_case(case)
            return self._case_view(case, actor)

    # ------------------------------------------------------------------
    # 查询（按权限裁剪）
    # ------------------------------------------------------------------

    def get_case(self, actor: Actor | dict[str, Any], case_id: str) -> dict[str, Any]:
        actor = self._actor(actor)
        case = self._load_case(case_id)
        self._require_view(actor, case)
        return self._case_view(case, actor)

    def list_cases(self, actor: Actor | dict[str, Any], *, booking_id: str | None = None) -> dict[str, Any]:
        actor = self._actor(actor)
        cases = [DisputeCase.from_dict(r) for r in self._cases.list_cases(booking_id=booking_id)]
        items: list[dict[str, Any]] = []
        for case in cases:
            if actor.has_any(INTERNAL_ROLES) or case.party_of(actor.actor_id) is not None:
                items.append(self._case_summary(case, actor))
        items.sort(key=lambda v: (v["created_at"], v["case_id"]))
        return {"items": items}

    def _require_view(self, actor: Actor, case: DisputeCase) -> None:
        if actor.has_any(INTERNAL_ROLES) or case.party_of(actor.actor_id) is not None:
            return
        raise PermissionDeniedError(
            "actor is not allowed to view this case",
            details={"case_id": case.case_id, "actor_id": actor.actor_id},
        )

    @staticmethod
    def _parse_party(request: dict[str, Any]) -> DisputeParty:
        raw = request.get("party")
        try:
            return DisputeParty(raw)
        except ValueError:
            raise ValidationError(
                "field party must be APPLICANT or RESPONDENT",
                details={"received": raw},
            )

    def _case_view(self, case: DisputeCase, actor: Actor) -> dict[str, Any]:
        """组装案件视图，并按主体角色裁剪陈述/证据。"""
        statements = [PartyStatement.from_dict(r) for r in self._cases.list_statements(case.case_id)]
        evidence = [CaseEvidence.from_dict(r) for r in self._cases.list_evidence(case.case_id)]
        decision_record = self._cases.get_decision(case.case_id)

        viewer_party = case.party_of(actor.actor_id)
        is_internal = actor.has_any(INTERNAL_ROLES)

        if is_internal:
            visible_statements = statements
            visible_evidence = evidence
        else:
            # 当事方：仅可见本方陈述与本方证据；处理决定对双方公开。
            visible_statements = [s for s in statements if s.party == viewer_party]
            visible_evidence = [e for e in evidence if e.party == viewer_party]

        statements.sort(key=lambda s: (s.created_at, s.statement_id))
        visible_evidence_list = sorted(visible_evidence, key=lambda e: (e.created_at, e.evidence_id))
        view = case.to_dict()
        view["statements"] = [s.to_dict() for s in visible_statements]
        view["evidence"] = [e.to_dict() for e in visible_evidence_list]
        view["decision"] = decision_record
        view["viewer"] = {
            "actor_id": actor.actor_id,
            "roles": sorted(actor.roles),
            "party": viewer_party.value if viewer_party is not None else None,
            "internal": is_internal,
        }
        view["counts"] = {
            "statements_visible": len(view["statements"]),
            "evidence_visible": len(view["evidence"]),
        }
        return view

    def _case_summary(self, case: DisputeCase, actor: Actor) -> dict[str, Any]:
        viewer_party = case.party_of(actor.actor_id)
        return {
            "case_id": case.case_id,
            "booking_id": case.booking_id,
            "title": case.title,
            "status": case.status.value,
            "applicant_id": case.applicant_id,
            "respondent_id": case.respondent_id,
            "created_at": dt_to_str(case.created_at),
            "closed_at": dt_to_str(case.closed_at) if case.closed_at else None,
            "has_decision": self._cases.get_decision(case.case_id) is not None,
            "viewer_party": viewer_party.value if viewer_party is not None else None,
        }
