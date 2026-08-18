"""Research backflow manual closure (P0 — Human Review closed loop).

When the top-level orchestration terminates at research_backflow_manual
(reason=research_backflow_limit_reached / research_backflow_no_progress or an
executor manual reason), the system must give the user a workable closed loop
instead of a waiting_human dead end:

- BackflowHumanReviewRequest: at most one persistence-backed review request per
  orchestration (immutable, fingerprint-verified; spec allows "HumanReviewRequest
  / equivalent persistent object");
- BackflowHumanReviewDecision: an immutable human adjudication for that request;
  decision in {accept / extra_research / cancel}:
    - accept: accept the current report (only when it holds no critical
      integrity failure);
    - extra_research: start one bounded manual supplemental research round
      (reuse the K2 same-thread resume; never unbounded);
    - cancel: end the task cleanly.

Principle: the model does not decide what can be accepted; only deterministic
guards (deterministic Check=pass AND no critical audit issue) allow accept.
Any adjudication never mutates Report / Audit / ReviewAction (a new immutable
artifact).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.errors import DomainError

BACKFLOW_REVIEW_SCHEMA_VERSION = 1
BACKFLOW_DECISION_SCHEMA_VERSION = 1

BACKFLOW_DECISION_ACCEPT = "accept"
BACKFLOW_DECISION_EXTRA_RESEARCH = "extra_research"
BACKFLOW_DECISION_CANCEL = "cancel"
BACKFLOW_DECISIONS = frozenset(
    {BACKFLOW_DECISION_ACCEPT, BACKFLOW_DECISION_EXTRA_RESEARCH, BACKFLOW_DECISION_CANCEL}
)


def compute_backflow_review_fingerprint(
    *,
    request_schema_version: int,
    orchestration_id: UUID,
    reason: str,
    request_payload: dict[str, Any],
) -> str:
    payload = {
        "request_schema_version": request_schema_version,
        "orchestration_id": str(orchestration_id),
        "reason": reason,
        "request_payload": request_payload,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_backflow_decision_fingerprint(
    *,
    decision_schema_version: int,
    backflow_human_request_id: UUID,
    request_fingerprint: str,
    decision: str,
    comment: str | None,
) -> str:
    payload = {
        "decision_schema_version": decision_schema_version,
        "backflow_human_request_id": str(backflow_human_request_id),
        "request_fingerprint": request_fingerprint,
        "decision": decision,
        "comment": comment,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_backflow_comment(comment: str | None) -> str | None:
    if comment is None:
        return None
    normalized = " ".join(str(comment).split())
    return normalized or None


@dataclass(frozen=True)
class BackflowHumanReviewRequestResult:
    backflow_human_request_id: UUID
    orchestration_id: UUID
    reason: str
    request_schema_version: int
    request_payload: dict[str, Any]
    request_fingerprint: str
    created_at: datetime
    replayed: bool = False


@dataclass(frozen=True)
class BackflowHumanReviewDecisionResult:
    backflow_human_decision_id: UUID
    backflow_human_request_id: UUID
    decision_schema_version: int
    decision: str
    comment: str | None
    decided_at: datetime
    decision_fingerprint: str
    replayed: bool = False


class BackflowClosureError(DomainError):
    """closure 域错误基类（DomainError → 统一错误信封，409）。"""

    code = "backflow_closure_error"
    http_status = 409
    message = "补充研究人工审核处理失败"


class BackflowReviewNotAcceptable(BackflowClosureError):
    """accept 被确定性守卫拒绝（按钮 disable + 中文理由）。"""

    code = "backflow_review_not_acceptable"
    http_status = 409

    def __init__(self, barriers: list[str]) -> None:
        self.barriers = barriers
        super().__init__("；".join(barriers))


class BackflowReviewAlreadyResolved(BackflowClosureError):
    code = "backflow_review_already_resolved"
    message = "该补充研究请求已处理，不能重复裁决"


class BackflowReviewNotFound(BackflowClosureError):
    code = "backflow_review_not_found"
    http_status = 404
    message = "补充研究人工审核请求不存在"


class BackflowClosureIntegrityError(BackflowClosureError):
    code = "backflow_closure_integrity_error"
    message = "补充研究人工审核数据完整性校验失败"


class ResearchBackflowClosureService:
    """Backflow manual closure persistence + adjudication (0 LLM; short tx)."""

    def __init__(self, sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_or_get_review(
        self,
        orchestration_id: UUID,
        *,
        reason: str,
        request_payload: dict[str, Any] | None = None,
    ) -> BackflowHumanReviewRequestResult:
        from app.db.models.research_backflow import BackflowHumanReviewRequestModel
        from app.repositories.backflow_review_repository import BackflowReviewRepository

        payload = dict(request_payload or {})
        fingerprint = compute_backflow_review_fingerprint(
            request_schema_version=BACKFLOW_REVIEW_SCHEMA_VERSION,
            orchestration_id=orchestration_id,
            reason=reason,
            request_payload=payload,
        )
        expected = BackflowHumanReviewRequestModel(
            backflow_human_request_id=uuid.uuid4(),
            orchestration_id=orchestration_id,
            reason=reason,
            request_schema_version=BACKFLOW_REVIEW_SCHEMA_VERSION,
            request_payload=payload,
            request_fingerprint=fingerprint,
        )
        async with self._sessionmaker() as session:
            row, was_created = await BackflowReviewRepository(session).create_or_get(expected)
            if not was_created and row.request_fingerprint != fingerprint:
                raise BackflowClosureIntegrityError("backflow review fingerprint mismatch")
            await session.commit()
        return BackflowHumanReviewRequestResult(
            backflow_human_request_id=row.backflow_human_request_id,
            orchestration_id=row.orchestration_id,
            reason=row.reason,
            request_schema_version=row.request_schema_version,
            request_payload=dict(row.request_payload),
            request_fingerprint=row.request_fingerprint,
            created_at=row.created_at,
            replayed=not was_created,
        )

    async def get_request_for_orchestration(
        self, orchestration_id: UUID
    ) -> BackflowHumanReviewRequestResult | None:
        from app.repositories.backflow_review_repository import BackflowReviewRepository

        async with self._sessionmaker() as session:
            row = await BackflowReviewRepository(session).get_by_orchestration(orchestration_id)
        if row is None:
            return None
        return BackflowHumanReviewRequestResult(
            backflow_human_request_id=row.backflow_human_request_id,
            orchestration_id=row.orchestration_id,
            reason=row.reason,
            request_schema_version=row.request_schema_version,
            request_payload=dict(row.request_payload),
            request_fingerprint=row.request_fingerprint,
            created_at=row.created_at,
        )

    async def get_decision_for_request(
        self, backflow_human_request_id: UUID
    ) -> BackflowHumanReviewDecisionResult | None:
        from app.repositories.backflow_review_repository import BackflowReviewRepository

        async with self._sessionmaker() as session:
            row = await BackflowReviewRepository(session).get_decision_by_request(
                backflow_human_request_id
            )
        if row is None:
            return None
        return BackflowHumanReviewDecisionResult(
            backflow_human_decision_id=row.backflow_human_decision_id,
            backflow_human_request_id=row.backflow_human_request_id,
            decision_schema_version=row.decision_schema_version,
            decision=row.decision,
            comment=row.comment,
            decided_at=row.decided_at,
            decision_fingerprint=row.decision_fingerprint,
        )

    async def resolve_review(
        self,
        backflow_human_request_id: UUID,
        *,
        decision: str,
        comment: str | None = None,
    ) -> BackflowHumanReviewDecisionResult:
        from app.db.models.research_backflow import BackflowHumanReviewDecisionModel
        from app.repositories.backflow_review_repository import BackflowReviewRepository

        if decision not in BACKFLOW_DECISIONS:
            raise BackflowClosureError(
                f"backflow decision must be one of {sorted(BACKFLOW_DECISIONS)}"
            )
        normalized_comment = normalize_backflow_comment(comment)

        async with self._sessionmaker() as session:
            req = await BackflowReviewRepository(session).get_by_id(backflow_human_request_id)
            if req is None:
                raise BackflowReviewNotFound()
            decision_fp = compute_backflow_decision_fingerprint(
                decision_schema_version=BACKFLOW_DECISION_SCHEMA_VERSION,
                backflow_human_request_id=backflow_human_request_id,
                request_fingerprint=req.request_fingerprint,
                decision=decision,
                comment=normalized_comment,
            )
            expected = BackflowHumanReviewDecisionModel(
                backflow_human_decision_id=uuid.uuid4(),
                backflow_human_request_id=backflow_human_request_id,
                decision_schema_version=BACKFLOW_DECISION_SCHEMA_VERSION,
                decision=decision,
                comment=normalized_comment,
                decided_at=datetime.now(UTC),
                decision_fingerprint=decision_fp,
            )
            row, was_created = await BackflowReviewRepository(session).create_or_get_decision(
                expected
            )
            if not was_created and (row.decision != decision or row.comment != normalized_comment):
                raise BackflowReviewAlreadyResolved()
            await session.commit()
        return BackflowHumanReviewDecisionResult(
            backflow_human_decision_id=row.backflow_human_decision_id,
            backflow_human_request_id=row.backflow_human_request_id,
            decision_schema_version=row.decision_schema_version,
            decision=row.decision,
            comment=row.comment,
            decided_at=row.decided_at,
            decision_fingerprint=row.decision_fingerprint,
            replayed=not was_created,
        )
