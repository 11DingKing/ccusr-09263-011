"""预约争议案件领域模型。

客服可把针对某条预约的升级投诉登记为“预约争议案件”。案件需要：

- 关联原预约（``booking_id``）；
- 分别留存申请方与被申请方的陈述（:class:`PartyStatement`）；
- 汇聚双方提交的证据（:class:`CaseEvidence`）；
- 在处理后落一条处理决定（:class:`CaseDecision`），决定作出即关闭案件。

案件关闭后仍允许查看，但不允许追加未经授权的证据；仅持处理权限的
主体在显式授权（``authorized=True``）下方可补证。时间与序列化约定与
:mod:`service_09252_008.domain.models` 一致（UTC、ISO-8601）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .models import dt_from_str, dt_to_str


class DisputeParty(str, Enum):
    """争议双方。"""

    APPLICANT = "APPLICANT"  # 申请方（提出升级投诉的院校）
    RESPONDENT = "RESPONDENT"  # 被申请方（导师/工坊侧）


class DisputeCaseStatus(str, Enum):
    OPEN = "OPEN"  # 处理中：可追加陈述与证据
    CLOSED = "CLOSED"  # 已关闭：仅可查看，补证须授权


#: 内部处理角色：可见案件全部材料
INTERNAL_ROLES = frozenset({"support_agent", "case_handler", "admin"})

#: 可登记案件的角色
CASE_OPEN_ROLES = frozenset({"support_agent", "case_handler", "admin"})

#: 可作出处理决定、关闭案件及授权补证的角色
CASE_DECIDE_ROLES = frozenset({"case_handler", "admin"})


@dataclass(frozen=True)
class Actor:
    """操作主体：标识 + 角色集合。"""

    actor_id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_any(self, roles: frozenset[str]) -> bool:
        return bool(self.roles & roles)

    def to_dict(self) -> dict[str, Any]:
        return {"actor_id": self.actor_id, "roles": sorted(self.roles)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Actor":
        return cls(actor_id=data["actor_id"], roles=frozenset(data.get("roles", ())))


@dataclass
class PartyStatement:
    """一方陈述。"""

    statement_id: str
    case_id: str
    party: DisputeParty
    author_id: str  # 实际陈述人（可能由客服代登记）
    content: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "case_id": self.case_id,
            "party": self.party.value,
            "author_id": self.author_id,
            "content": self.content,
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PartyStatement":
        return cls(
            statement_id=data["statement_id"],
            case_id=data["case_id"],
            party=DisputeParty(data["party"]),
            author_id=data["author_id"],
            content=data["content"],
            created_at=dt_from_str(data["created_at"]),
        )


@dataclass
class CaseEvidence:
    """案件证据。

    ``content_fingerprint`` 为证据内容的 SHA-256，用于留存与比对；
    ``authorized`` 标记该证据是否为案件关闭后经授权补交。
    """

    evidence_id: str
    case_id: str
    party: DisputeParty
    submitter_id: str
    kind: str  # 证据类型：document / photo / recording / other
    description: str
    content_ref: str  # 外部内容定位（对象存储键/材料编号等）
    content_fingerprint: str
    authorized: bool
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "case_id": self.case_id,
            "party": self.party.value,
            "submitter_id": self.submitter_id,
            "kind": self.kind,
            "description": self.description,
            "content_ref": self.content_ref,
            "content_fingerprint": self.content_fingerprint,
            "authorized": self.authorized,
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseEvidence":
        return cls(
            evidence_id=data["evidence_id"],
            case_id=data["case_id"],
            party=DisputeParty(data["party"]),
            submitter_id=data["submitter_id"],
            kind=data["kind"],
            description=data["description"],
            content_ref=data["content_ref"],
            content_fingerprint=data["content_fingerprint"],
            authorized=bool(data.get("authorized", False)),
            created_at=dt_from_str(data["created_at"]),
        )


@dataclass
class CaseDecision:
    """处理决定：一案一条，落定即关闭案件。"""

    decision_id: str
    case_id: str
    handler_id: str
    outcome: str  # 结论分类：uphold / reject / partial / mediate
    summary: str
    remedy: str | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "case_id": self.case_id,
            "handler_id": self.handler_id,
            "outcome": self.outcome,
            "summary": self.summary,
            "remedy": self.remedy,
            "created_at": dt_to_str(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaseDecision":
        return cls(
            decision_id=data["decision_id"],
            case_id=data["case_id"],
            handler_id=data["handler_id"],
            outcome=data["outcome"],
            summary=data["summary"],
            remedy=data.get("remedy"),
            created_at=dt_from_str(data["created_at"]),
        )


@dataclass
class DisputeCase:
    """预约争议案件聚合头。"""

    case_id: str
    booking_id: str
    title: str
    opened_by: str  # 登记客服
    applicant_id: str  # 申请方主体标识
    respondent_id: str  # 被申请方主体标识
    status: DisputeCaseStatus
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None

    def party_of(self, actor_id: str) -> DisputeParty | None:
        """返回主体在本案中的当事方身份；非当事人返回 ``None``。"""
        if actor_id == self.applicant_id:
            return DisputeParty.APPLICANT
        if actor_id == self.respondent_id:
            return DisputeParty.RESPONDENT
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "booking_id": self.booking_id,
            "title": self.title,
            "opened_by": self.opened_by,
            "applicant_id": self.applicant_id,
            "respondent_id": self.respondent_id,
            "status": self.status.value,
            "created_at": dt_to_str(self.created_at),
            "updated_at": dt_to_str(self.updated_at),
            "closed_at": dt_to_str(self.closed_at) if self.closed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisputeCase":
        return cls(
            case_id=data["case_id"],
            booking_id=data["booking_id"],
            title=data["title"],
            opened_by=data["opened_by"],
            applicant_id=data["applicant_id"],
            respondent_id=data["respondent_id"],
            status=DisputeCaseStatus(data["status"]),
            created_at=dt_from_str(data["created_at"]),
            updated_at=dt_from_str(data["updated_at"]),
            closed_at=dt_from_str(data["closed_at"]) if data.get("closed_at") else None,
        )
