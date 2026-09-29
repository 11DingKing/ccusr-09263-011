"""预约争议案件服务：立案、双方陈述、证据提交、处理决定与按权限裁剪的查询。

约定：
- 只有客服（``support``）可以把升级投诉登记为案件，案件必须关联存在的原预约；
- 双方陈述与证据在案件 OPEN 期间可提交；院校/导师只能操作与本预约相关的内容；
- 仲裁（``arbiter``）作出处理决定后案件关闭；
- 关闭后案件只读：仍可查看，但追加证据一律拒绝，除非仲裁出示显式授权；
- 查询在 Python 层按 :class:`~service_09252_008.domain.disputes.Principal`
  裁剪机密证据、内部笔录与决定内部理由。
"""
from __future__ import annotations

from typing import Any

from ..domain.disputes import (
    INTERNAL_ROLES,
    CaseDecision,
    CaseEvidence,
    CaseOutcome,
    CaseStatus,
    DisputeCase,
    DisputeParty,
    EvidenceAuthorization,
    PartyStatement,
    Principal,
)
from ..domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    StateError,
    ValidationError,
)
from ..domain.models import DomainEvent, dt_to_str
from ..persistence.store import Store
from .booking_service import COLLECTION_BOOKINGS, COLLECTION_EVENTS
from .ports import Clock, IdGenerator

COLLECTION_CASES = "dispute_cases"
COLLECTION_STATEMENTS = "dispute_statements"
COLLECTION_EVIDENCE = "dispute_evidence"
COLLECTION_DECISIONS = "dispute_decisions"

_BOOKING_SNAPSHOT_FIELDS = (
    "booking_id",
    "institution",
    "package_id",
    "mentor_id",
    "resource_id",
    "window_id",
    "seats",
    "slot_start",
    "slot_end",
    "status",
    "created_at",
)


class DisputeCaseService:
    """争议案件用例编排。"""

    def __init__(self, store: Store, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _emit(self, event_type: str, case_id: str, payload: dict[str, Any]) -> None:
        event = DomainEvent(
            event_id=self._ids.new_id("evt"),
            type=event_type,
            booking_id=None,
            payload={"case_id": case_id, **payload},
            created_at=self._clock.now(),
        )
        self._store.put(COLLECTION_EVENTS, event.event_id, event.to_dict())

    def _load_case(self, case_id: str) -> DisputeCase:
        record = self._store.get(COLLECTION_CASES, case_id)
        if record is None:
            raise NotFoundError(f"dispute case not found: {case_id}", details={"case_id": case_id})
        return DisputeCase.from_dict(record)

    def _save_case(self, case: DisputeCase) -> None:
        case.version += 1
        self._store.put(COLLECTION_CASES, case.case_id, case.to_dict())

    def _statements(self, case_id: str) -> list[PartyStatement]:
        rows = self._store.query(COLLECTION_STATEMENTS, case_id=case_id)
        statements = [PartyStatement.from_dict(r) for r in rows]
        statements.sort(key=lambda s: (s.created_at, s.statement_id))
        return statements

    def _evidence(self, case_id: str) -> list[CaseEvidence]:
        rows = self._store.query(COLLECTION_EVIDENCE, case_id=case_id)
        items = [CaseEvidence.from_dict(r) for r in rows]
        items.sort(key=lambda e: (e.created_at, e.evidence_id))
        return items

    def _decision(self, case_id: str) -> CaseDecision | None:
        rows = self._store.query(COLLECTION_DECISIONS, case_id=case_id)
        if not rows:
            return None
        return CaseDecision.from_dict(rows[0])

    def _ensure_case_participant(self, case: DisputeCase, principal: Principal) -> None:
        """院校/导师只能访问与自身相关的案件；办案组不受限。"""
        if principal.is_internal:
            return
        snapshot = case.booking_snapshot
        if principal.role == "institution" and principal.ref == snapshot.get("institution"):
            return
        if principal.role == "mentor" and principal.ref == snapshot.get("mentor_id"):
            return
        raise PermissionDeniedError(
            "principal is not a participant of this case",
            details={"case_id": case.case_id, "principal": str(principal)},
        )

    # ------------------------------------------------------------------
    # 立案
    # ------------------------------------------------------------------

    def open_case(self, request: dict[str, Any], principal: Principal) -> dict[str, Any]:
        """客服把升级投诉登记为争议案件，关联原预约并留存预约快照。"""
        if principal.role != "support":
            raise PermissionDeniedError(
                "only support staff can register dispute cases",
                details={"principal": str(principal)},
            )
        booking_id = request.get("booking_id")
        if not isinstance(booking_id, str) or not booking_id.strip():
            raise ValidationError("field booking_id must be a non-empty string")
        booking = self._store.get(COLLECTION_BOOKINGS, booking_id.strip())
        if booking is None:
            raise NotFoundError(
                f"booking not found: {booking_id}", details={"booking_id": booking_id}
            )
        booking_id = booking_id.strip()
        title = request.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("field title must be a non-empty string")
        category = request.get("category")
        if not isinstance(category, str) or not category.strip():
            raise ValidationError("field category must be a non-empty string")

        now = self._clock.now()
        case = DisputeCase(
            case_id=self._ids.new_id("cse"),
            booking_id=booking_id,
            booking_snapshot={field: booking.get(field) for field in _BOOKING_SNAPSHOT_FIELDS},
            title=title.strip(),
            category=category.strip(),
            status=CaseStatus.OPEN,
            opened_by=str(principal),
            opened_at=now,
        )
        with self._store.transaction():
            self._save_case(case)
            self._emit(
                "dispute_case_opened",
                case.case_id,
                {"booking_id": booking_id, "category": case.category},
            )
        return self.get_case(case.case_id, principal)

    # ------------------------------------------------------------------
    # 双方陈述
    # ------------------------------------------------------------------

    def add_statement(self, case_id: str, request: dict[str, Any], principal: Principal) -> dict[str, Any]:
        content = request.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("field content must be a non-empty string")
        internal = bool(request.get("internal", False))
        author = request.get("author")
        if author is not None and (not isinstance(author, str) or not author.strip()):
            raise ValidationError("field author must be a non-empty string when provided")

        with self._store.transaction():
            case = self._load_case(case_id)
            self._ensure_case_participant(case, principal)
            if case.closed:
                raise StateError(
                    "case is closed; statements can no longer be added",
                    details={"case_id": case_id, "status": case.status.value},
                )

            if principal.role == "institution":
                party = DisputeParty.INSTITUTION
                if internal:
                    raise PermissionDeniedError("parties cannot file internal statements")
                author = author or principal.ref
            elif principal.role == "mentor":
                party = DisputeParty.MENTOR
                if internal:
                    raise PermissionDeniedError("parties cannot file internal statements")
                author = author or principal.ref
            else:
                # 客服/仲裁可代任一方录陈述；必须显式指明 party
                raw_party = request.get("party")
                if raw_party not in (DisputeParty.INSTITUTION.value, DisputeParty.MENTOR.value):
                    raise ValidationError(
                        "field party must be INSTITUTION or MENTOR when support records a statement"
                    )
                party = DisputeParty(raw_party)
                author = author or raw_party

            statement = PartyStatement(
                statement_id=self._ids.new_id("stm"),
                case_id=case_id,
                party=party,
                author=author,
                content=content.strip(),
                recorded_by=str(principal),
                internal=internal,
                created_at=self._clock.now(),
            )
            self._store.put(COLLECTION_STATEMENTS, statement.statement_id, statement.to_dict())
            self._emit(
                "dispute_statement_added",
                case_id,
                {"statement_id": statement.statement_id, "party": party.value, "internal": internal},
            )
        return self.get_case(case_id, principal)

    # ------------------------------------------------------------------
    # 证据提交（关闭后冻结，授权补录除外）
    # ------------------------------------------------------------------

    def add_evidence(self, case_id: str, request: dict[str, Any], principal: Principal) -> dict[str, Any]:
        title = request.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("field title must be a non-empty string")
        evidence_type = request.get("evidence_type", "document")
        if not isinstance(evidence_type, str) or not evidence_type.strip():
            raise ValidationError("field evidence_type must be a non-empty string")
        description = request.get("description", "")
        if not isinstance(description, str):
            raise ValidationError("field description must be a string")
        confidential = bool(request.get("confidential", False))
        if confidential and not principal.is_internal:
            raise PermissionDeniedError("only the case team can file confidential evidence")

        with self._store.transaction():
            case = self._load_case(case_id)
            self._ensure_case_participant(case, principal)

            authorization: EvidenceAuthorization | None = None
            if case.closed:
                authorization = self._parse_after_close_authorization(request, principal)

            evidence = CaseEvidence(
                evidence_id=self._ids.new_id("evd"),
                case_id=case_id,
                title=title.strip(),
                evidence_type=evidence_type.strip(),
                description=description.strip(),
                submitted_by=str(principal),
                submitter_party=principal.party,
                confidential=confidential,
                authorized_after_close=authorization is not None,
                authorization=authorization,
                created_at=self._clock.now(),
            )
            self._store.put(COLLECTION_EVIDENCE, evidence.evidence_id, evidence.to_dict())
            self._emit(
                "dispute_evidence_added",
                case_id,
                {
                    "evidence_id": evidence.evidence_id,
                    "confidential": confidential,
                    "authorized_after_close": evidence.authorized_after_close,
                },
            )
        return self.get_case(case_id, principal)

    def _parse_after_close_authorization(
        self, request: dict[str, Any], principal: Principal
    ) -> EvidenceAuthorization:
        """关闭案件补证：必须由仲裁持显式授权办理，否则拒绝。"""
        raw = request.get("authorization")
        if not isinstance(raw, dict):
            raise StateError(
                "case is closed; adding evidence requires an explicit arbiter authorization",
                details={"authorization_required": True},
            )
        if principal.role != "arbiter":
            raise PermissionDeniedError(
                "after-close evidence can only be filed by an arbiter holding authorization",
                details={"principal": str(principal)},
            )
        granted_by_raw = raw.get("granted_by")
        reason = raw.get("reason")
        if not isinstance(granted_by_raw, str) or not granted_by_raw.strip():
            raise ValidationError("authorization.granted_by must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("authorization.reason must be a non-empty string")
        try:
            granter = Principal.parse(granted_by_raw)
        except ValueError as exc:
            raise ValidationError(f"invalid authorization.granted_by: {exc}") from exc
        if granter.role != "arbiter":
            raise ValidationError("evidence after close must be authorized by an arbiter")
        return EvidenceAuthorization(
            granted_by=str(granter),
            reason=reason.strip(),
            authorized_at=self._clock.now(),
        )

    # ------------------------------------------------------------------
    # 处理决定（关闭案件）
    # ------------------------------------------------------------------

    def decide(self, case_id: str, request: dict[str, Any], principal: Principal) -> dict[str, Any]:
        if principal.role != "arbiter":
            raise PermissionDeniedError("only an arbiter can issue a decision")
        outcome_raw = request.get("outcome")
        try:
            outcome = CaseOutcome(outcome_raw)
        except ValueError:
            allowed = ", ".join(o.value for o in CaseOutcome)
            raise ValidationError(f"field outcome must be one of: {allowed}")
        summary = request.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValidationError("field summary must be a non-empty string")
        actions = request.get("actions", [])
        if not isinstance(actions, list) or any(not isinstance(a, str) or not a.strip() for a in actions):
            raise ValidationError("field actions must be a list of non-empty strings")
        internal_rationale = request.get("internal_rationale")
        if internal_rationale is not None and (
            not isinstance(internal_rationale, str) or not internal_rationale.strip()
        ):
            raise ValidationError("field internal_rationale must be a non-empty string when provided")

        with self._store.transaction():
            case = self._load_case(case_id)
            if case.closed:
                raise StateError(
                    "case is already closed",
                    details={"case_id": case_id, "decision_id": case.decision_id},
                )
            now = self._clock.now()
            decision = CaseDecision(
                decision_id=self._ids.new_id("dec"),
                case_id=case_id,
                outcome=outcome,
                summary=summary.strip(),
                actions=[a.strip() for a in actions],
                internal_rationale=internal_rationale.strip() if internal_rationale else None,
                decided_by=str(principal),
                decided_at=now,
            )
            self._store.put(COLLECTION_DECISIONS, decision.decision_id, decision.to_dict())
            case.status = CaseStatus.CLOSED
            case.decision_id = decision.decision_id
            case.closed_at = now
            self._save_case(case)
            self._emit(
                "dispute_case_decided",
                case_id,
                {"decision_id": decision.decision_id, "outcome": outcome.value},
            )
        return self.get_case(case_id, principal)

    # ------------------------------------------------------------------
    # 查询（按权限裁剪）
    # ------------------------------------------------------------------

    def list_cases(self, principal: Principal) -> dict[str, Any]:
        cases = [DisputeCase.from_dict(r) for r in self._store.query(COLLECTION_CASES)]
        cases.sort(key=lambda c: (c.opened_at, c.case_id))
        items: list[dict[str, Any]] = []
        for case in cases:
            try:
                self._ensure_case_participant(case, principal)
            except PermissionDeniedError:
                continue
            items.append(self._case_view(case, principal, include_children=False))
        return {"items": items}

    def get_case(self, case_id: str, principal: Principal) -> dict[str, Any]:
        case = self._load_case(case_id)
        self._ensure_case_participant(case, principal)
        return self._case_view(case, principal, include_children=True)

    def _case_view(self, case: DisputeCase, principal: Principal, *, include_children: bool) -> dict[str, Any]:
        view = case.to_dict()
        view["viewer"] = str(principal)
        if include_children:
            statements = self._statements(case.case_id)
            if principal.is_internal:
                view["statements"] = [s.to_dict() for s in statements]
            else:
                # 双方看不到办案组内部笔录
                view["statements"] = [s.to_dict() for s in statements if not s.internal]

            evidence = self._evidence(case.case_id)
            if principal.is_internal:
                view["evidence"] = [e.to_dict() for e in evidence]
            else:
                # 双方看不到机密证据
                view["evidence"] = [e.to_dict() for e in evidence if not e.confidential]

            decision = self._decision(case.case_id)
            view["decision"] = self._decision_view(decision, principal)
        return view

    def _decision_view(self, decision: CaseDecision | None, principal: Principal) -> dict[str, Any] | None:
        if decision is None:
            return None
        data = decision.to_dict()
        if not principal.is_internal:
            # 内部理由不对双方公开
            data["internal_rationale"] = None
        return data
