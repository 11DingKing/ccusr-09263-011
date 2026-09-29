"""预约争议案件领域模型。

客服把升级投诉登记为争议案件。案件必须关联原预约，并汇集：

- 双方陈述（申请院校 / 授课导师）；
- 案件证据（客服、仲裁与双方均可提交，部分材料仅限办案组查看）；
- 处理决定（仲裁作出，与案件拆表保存）。

案件关闭后只读：仍可查看，但证据冻结，只有出示显式授权的仲裁才能补录。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .models import dt_from_str, dt_to_str

#: 办案组内部角色：可见机密证据、内部笔录与决定的内部理由
INTERNAL_ROLES = frozenset({"support", "arbiter"})


class CaseStatus(str, Enum):
    OPEN = "OPEN"  # 办理中：可录陈述、补证据
    CLOSED = "CLOSED"  # 已关闭：只读，证据冻结（授权补录除外）


class DisputeParty(str, Enum):
    INSTITUTION = "INSTITUTION"  # 申请院校
    MENTOR = "MENTOR"  # 授课导师
    SUPPORT = "SUPPORT"  # 客服
    ARBITER = "ARBITER"  # 仲裁/主管


class CaseOutcome(str, Enum):
    FOR_INSTITUTION = "FOR_INSTITUTION"  # 裁定支持院校
    FOR_MENTOR = "FOR_MENTOR"  # 裁定支持导师
    COMPROMISE = "COMPROMISE"  # 双方各让一步
    WITHDRAWN = "WITHDRAWN"  # 投诉撤回


@dataclass(frozen=True)
class Principal:
    """操作者身份。

    ``role`` 为 ``support`` / ``arbiter`` 时 ``ref`` 为空；
    为 ``institution`` / ``mentor`` 时 ``ref`` 分别是院校名称或导师 ID。
    文本形如 ``support``、``institution:城南大学``、``mentor:m_0001``。
    """

    role: str
    ref: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Principal":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("principal must be a non-empty string")
        raw = text.strip()
        if ":" in raw:
            role, ref = raw.split(":", 1)
            role = role.strip()
            ref = ref.strip()
        else:
            role, ref = raw.strip(), None
        if role in INTERNAL_ROLES:
            if ref:
                raise ValueError(f"{role} principal must not carry a ref")
        elif role in ("institution", "mentor"):
            if not ref:
                raise ValueError(f"{role} principal requires a ref, e.g. {role}:<id>")
        else:
            raise ValueError(f"unknown principal role: {role}")
        return cls(role, ref or None)

    @property
    def is_internal(self) -> bool:
        return self.role in INTERNAL_ROLES

    @property
    def party(self) -> DisputeParty:
        mapping = {
            "support": DisputeParty.SUPPORT,
            "arbiter": DisputeParty.ARBITER,
            "institution": DisputeParty.INSTITUTION,
            "mentor": DisputeParty.MENTOR,
        }
        return mapping[self.role]

    def __str__(self) -> str:
        return self.role if self.ref is None else f"{self.role}:{self.ref}"


@dataclass
class DisputeCase:
    """争议案件：关联原预约的投诉升级工单。"""

    case_id: str
    booking_id: str
    booking_snapshot: dict[str, Any]  # 立案时的预约快照，预约后续变化不影响案件查看
    title: str
    category: str
    status: CaseStatus
    opened_by: str
    opened_at: Any  # datetime
    closed_at: Any | None = None
    decision_id: str | None = None
    version: int = 0

    @property
    def closed(self) -> bool:
        return self.status == CaseStatus.CLOSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "booking_id": self.booking_id,
            "booking_snapshot": dict(self.booking_snapshot),
            "title": self.title,
            "category": self.category,
            "status": self.status.value,
            "opened_by": self.opened_by,
            "opened_at": dt_to_str(self.opened_at),
            "closed_at": dt_to_str(self.closed_at) if self.closed_at else None,
            "decision_id": self.decision_id,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisputeCase":
        return cls(
            case_id=data["case_id"],
            booking_id=data["booking_id"],
            booking_snapshot=dict(data.get("booking_snapshot", {})),
            title=data["title"],
            category=data["category"],
            status=CaseStatus(data["status"]),
            opened_by=data["opened_by"],
            opened_at=dt_from_str(data["opened_at"]),
            closed_at=dt_from_str(data["closed_at"]) if data.get("closed_at") else None,
            decision_id=data.get("decision_id"),
            version=int(data.get("version", 0)),
        )


@dataclass
class PartyStatement:
    """双方陈述：客服可代录，``internal=True`` 的笔录仅办案组可见。"""

    statement_id: str
    case_id: str
    party: DisputeParty  # 仅 INSTITUTION / MENTOR
    author: str
    content: str
    recorded_by: str
    created_at: Any
    internal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "case_id": self.case_id,
            "party": self.party.value,
            "author": self.author,
            "content": self.content,
            "recorded_by": self.recorded_by,
            "internal": self.internal,
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PartyStatement":
        return cls(
            statement_id=data["statement_id"],
            case_id=data["case_id"],
            party=DisputeParty(data["party"]),
            author=data["author"],
            content=data["content"],
            recorded_by=data["recorded_by"],
            internal=bool(data.get("internal", False)),
            created_at=dt_from_str(data["created_at"]),
        )


@dataclass
class EvidenceAuthorization:
    """案件关闭后补录证据所需的显式授权。"""

    granted_by: str
    reason: str
    authorized_at: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted_by": self.granted_by,
            "reason": self.reason,
            "authorized_at": dt_to_str(self.authorized_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceAuthorization":
        return cls(
            granted_by=data["granted_by"],
            reason=data["reason"],
            authorized_at=dt_from_str(data["authorized_at"]),
        )


@dataclass
class CaseEvidence:
    """案件证据：独立于案件与决定保存。"""

    evidence_id: str
    case_id: str
    title: str
    evidence_type: str  # document / photo / receipt / recording ...
    submitted_by: str
    submitter_party: DisputeParty
    created_at: Any
    description: str = ""
    confidential: bool = False  # 机密材料：仅办案组可见
    authorized_after_close: bool = False
    authorization: EvidenceAuthorization | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "case_id": self.case_id,
            "title": self.title,
            "evidence_type": self.evidence_type,
            "description": self.description,
            "submitted_by": self.submitted_by,
            "submitter_party": self.submitter_party.value,
            "confidential": self.confidential,
            "authorized_after_close": self.authorized_after_close,
            "authorization": self.authorization.to_dict() if self.authorization else None,
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseEvidence":
        return cls(
            evidence_id=data["evidence_id"],
            case_id=data["case_id"],
            title=data["title"],
            evidence_type=data["evidence_type"],
            description=data.get("description", ""),
            submitted_by=data["submitted_by"],
            submitter_party=DisputeParty(data["submitter_party"]),
            confidential=bool(data.get("confidential", False)),
            authorized_after_close=bool(data.get("authorized_after_close", False)),
            authorization=(
                EvidenceAuthorization.from_dict(data["authorization"])
                if data.get("authorization")
                else None
            ),
            created_at=dt_from_str(data["created_at"]),
        )


@dataclass
class CaseDecision:
    """处理决定：仲裁关闭案件时写入，独立成表。"""

    decision_id: str
    case_id: str
    outcome: CaseOutcome
    summary: str
    decided_by: str
    decided_at: Any
    actions: list[str] | None = None
    internal_rationale: str | None = None  # 仅办案组可见

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "case_id": self.case_id,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "actions": list(self.actions or []),
            "internal_rationale": self.internal_rationale,
            "decided_by": self.decided_by,
            "decided_at": dt_to_str(self.decided_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseDecision":
        return cls(
            decision_id=data["decision_id"],
            case_id=data["case_id"],
            outcome=CaseOutcome(data["outcome"]),
            summary=data["summary"],
            actions=list(data.get("actions", [])),
            internal_rationale=data.get("internal_rationale"),
            decided_by=data["decided_by"],
            decided_at=dt_from_str(data["decided_at"]),
        )
