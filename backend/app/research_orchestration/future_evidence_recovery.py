"""P0.5 bounded FutureEvidence recovery (research isolation closure).

保留 synthesis no-lookahead guard **不变**；当 Stage4 因 `SynthesisFutureEvidence`
失败时，本服务做有界自恢复：

1. 从 Stage4 child checkpoint 读本次分析的 claim_ids；
2. 用 synthesis 同一 temporal policy 找出污染 claim（evidence availability 或
   domain as_of > cutoff）；
3. 把污染 claim 标记 invalid（`claims.invalidated_at`，**不删除证据/不删 claim**）；
4. orchestration 记录恢复尝试计数（bounded：超过 MAX 不再尝试 → SYSTEM_FAILURE）；
5. 返回 True → 顶层 graph 重新 resume Stage4：synthesis 输入加载时**排除**
   invalidated claim → 不再触发 guard。

- 不做任何日期改写 / 不删除全局证据 / 不放松 guard；
- 持续污染（每次 resume 仍有未来证据）→ 尝试耗尽 → 返回 False，
  由调用方投影 orchestration failed（SYSTEM_FAILURE）。
"""

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.repositories.claim_repository import ClaimRepository
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.synthesis.errors import SynthesisFutureEvidence

logger = get_logger("app.future_evidence_recovery")

MAX_FUTURE_EVIDENCE_RECOVERY_ATTEMPTS = 2
_INVALIDATION_REASON = "future_evidence_recovery"


class FutureEvidenceRecoveryService:
    """Stage4 SynthesisFutureEvidence 的有界自恢复（invalidate + bounded retry）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        stage4_runner,
        synthesis_service,
        orchestration_checkpoint_reader=None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._stage4_runner = stage4_runner
        self._synthesis_service = synthesis_service
        self._orchestration_checkpoint_reader = orchestration_checkpoint_reader

    async def try_recover(self, orchestration_id: UUID, exc: Exception) -> bool:
        """SynthesisFutureEvidence → 找到并 invalidate 污染 claim，返回是否可重试。

        非 FutureEvidence 异常 / 无污染 claim / 尝试次数耗尽 → False（不恢复）。
        """
        if not isinstance(exc, SynthesisFutureEvidence):
            return False
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
            if orchestration is None:
                return False
            attempts = getattr(orchestration, "future_evidence_recovery_attempts", 0) or 0
            if attempts >= MAX_FUTURE_EVIDENCE_RECOVERY_ATTEMPTS:
                logger.warning(
                    "future_evidence_recovery_exhausted",
                    orchestration_id=str(orchestration_id),
                    attempts=attempts,
                )
                return False
        # 读 Stage4 child checkpoint → claim_ids / analysis_as_of
        try:
            child_run_id = await self._child_run_id(orchestration_id)
            checkpoint = await self._stage4_runner.read_checkpoint_state(child_run_id)
        except Exception:
            return False
        claim_ids = [UUID(c) for c in (checkpoint.get("claim_ids") or [])]
        if not claim_ids:
            return False
        try:
            analysis_as_of = checkpoint["analysis_as_of"]
            if isinstance(analysis_as_of, str):
                analysis_as_of = date.fromisoformat(analysis_as_of)
        except (KeyError, ValueError):
            return False
        async with self._sessionmaker() as session:
            offending = await self._synthesis_service.find_future_evidence_claim_ids(
                session, claim_ids, analysis_as_of
            )
            if not offending:
                return False
            marked = await ClaimRepository(session).mark_invalidated(
                offending, _INVALIDATION_REASON
            )
            await ResearchOrchestrationRepository(session).increment_future_recovery_attempts(
                orchestration_id
            )
            await session.commit()
        logger.info(
            "future_evidence_recovery_invalidated",
            orchestration_id=str(orchestration_id),
            invalidated_claims=marked,
        )
        return True

    async def _child_run_id(self, orchestration_id: UUID) -> UUID:
        """顶层 checkpoint 的 stage4_child_run_id（无则 current_child_run_id）。"""
        if self._orchestration_checkpoint_reader is None:
            raise ValueError("orchestration checkpoint reader not injected")
        checkpoint = await self._orchestration_checkpoint_reader(orchestration_id)
        raw = checkpoint.get("stage4_child_run_id") or checkpoint.get("current_child_run_id")
        if not raw:
            raise ValueError("stage4 child run id missing")
        return UUID(str(raw))
