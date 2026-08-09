"""Financial claim service (stage 4B.2C.1): provenance + persistence + replay.

`create_claim(draft)` / `create_claim_batch(drafts)` 把**引用已登记
FinancialCalculation** 的 Financial Claim 确定性登记为 Claim +
ClaimEvidenceLink（自动展开 source Evidence）+
ClaimFinancialCalculationLink，形成 **Claim → ClaimFinancialCalculationLink →
FinancialCalculation → FinancialMetricObservation → EvidenceCard → Source**
完整可重算证据链。**0 LLM / 0 Chroma / 0 Report / 0 Audit / 0 LangGraph**。

流程（两步提交结构，镜像 ClaimService / FinancialCalculationService）：
1. 短 DB session 从真实 PG 加载全部 Calculation refs 并**逐条重放校验**
   （缺失 → FinancialClaimCalculationNotFound；company != draft →
   FinancialClaimCalculationMismatch；重放损坏 → FinancialClaimIntegrityError，
   **不 repair**），再加载 inputs → Observations（company 一致）→ 自动展开
   source Evidence（company 一致），随后立即关闭 connection（纯函数阶段不持有
   DB 连接）。
2. 纯函数派生（无 DB）：
   - **自动 Evidence expansion**：调用方/未来 LLM 只选 Calculation refs；程序
     加载每个 Calculation 的 source Evidence；
   - **relation semantics（schema 版本化）**：
     * v3（默认）：Calculation 承担 supports/contradicts/context 的语义关系，
       automatic source Evidence 展开到 ClaimEvidenceLinks 时**一律
       relation=context**（不是 propagation）；additional Evidence 保持调用方
       指定的 relation。
     * v2（legacy replay）：沿用原 relation propagation（calculation relation →
       source Evidence 同 relation）。
   - **relation conflict**：同一 Evidence 因多个 Calculations / additional
     Evidence 被推导成不同 relation → FinancialClaimRelationConflict（不静默
     选一个）；
   - **critical policy（schema 版本化）**：
     * v3：critical financial Claim 不能再依赖 ClaimEvidenceLink.supports 中的
       automatic source Evidence（现在都是 context）。deterministic source
       policy——至少满足其一：① 任一 support Calculation 展开得到的 source
       Evidence 中存在 critical_claim_eligible_snapshot=true；②
       additional_support_evidence_ids 中存在该属性。**Calculation 本身绝不
       提升 authority**。否则 FinancialClaimCriticalEvidenceInsufficient。
     * v2：沿用旧 policy（最终 supports evidence links 中任一 eligible）。
   - **v3 fingerprint**：claim_schema_version + claim semantic fields +
     evidence links + calculation links/fingerprints（v2/v3 不 collision）。
3. 短 DB transaction：create_or_get（ON CONFLICT(claim_fingerprint)，无进程锁）
   → created=True 时 bulk insert evidence links + calculation links；created=False
   时 **重新加载 Claim / links / Calculations / inputs / Observations /
   EvidenceCards 并重新派生逐项核实**（任一损坏 → FinancialClaimIntegrityError，
   **不自动 repair**）。任何 SQLAlchemyError → 整条 rollback +
   FinancialClaimPersistenceFailed（0 partial write）。并发 → 最终 1 Claim + 1
   套 links。无 update API（修改 = 新 Claim = 新 fingerprint = 新行）。
   create_claim_batch 为 **all-drafts-validate-first + 单 transaction**（任一
   draft 校验失败 → 整批拒绝，0 写；items 按 input drafts 顺序返回）。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph 分析节点。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.contracts import ClaimAnalysisDomain, compute_research_question_sha256
from app.claims.financial_contracts import (
    FINANCIAL_CLAIM_SCHEMA_VERSION_V2,
    FinancialClaimDraft,
    FinancialClaimImportance,
    compute_financial_claim_fingerprint,
)
from app.claims.financial_errors import (
    FinancialClaimCalculationMismatch,
    FinancialClaimCalculationNotFound,
    FinancialClaimCriticalEvidenceInsufficient,
    FinancialClaimDraftError,
    FinancialClaimEvidenceCompanyMismatch,
    FinancialClaimIntegrityError,
    FinancialClaimPersistenceFailed,
    FinancialClaimRelationConflict,
)
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.claim_financial_calculation_link import ClaimFinancialCalculationLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.financial.calculations.errors import FinancialCalculationError
from app.financial.calculations.service import FinancialCalculationService
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_financial_calculation_link_repository import (
    ClaimFinancialCalculationLinkRepository,
)
from app.repositories.claim_repository import ClaimRepository
from app.repositories.financial_calculation_input_repository import (
    FinancialCalculationInputRepository,
)
from app.repositories.financial_metric_observation_repository import (
    FinancialMetricObservationRepository,
)

_RELATIONS = ("supports", "contradicts", "context")

# 单次 create_claim_batch 最多 3 条 Financial Claim（与 4B.2C.2 的
# MAX_CLAIMS_PER_DECISION 一致）。
MAX_FINANCIAL_CLAIMS_PER_BATCH = 3


@dataclass(frozen=True)
class FinancialClaimResult:
    """一次 create_claim 的结果摘要（不含任何正文文本 / 数值细节）。"""

    claim_id: UUID
    claim_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class FinancialClaimBatchItem:
    """batch 中单个 draft 的结果（ordinal 从 1 开始，与 input drafts 一一对应）。

    - ordinal：draft 在本次 batch 中的位置（1..len(drafts)）；
    - claim_id：created 或 replayed 后的 Claim id；
    - replayed：True=复用既有 fingerprint 的 Claim，False=本次真正新增。
    """

    ordinal: int
    claim_id: UUID
    replayed: bool


@dataclass(frozen=True)
class FinancialClaimBatchResult:
    """一次 create_claim_batch 的结果摘要（不含任何正文文本 / evidence）。

    - items：**ordered result**——按 input drafts 顺序的逐条结果，
      len(items) == len(drafts)，items[i] 永远对应 drafts[i]（不按
      created/replayed 分组重排）；
    - fingerprints：claim_id → claim_fingerprint（供上游追溯）；
    - claim_ids / created / replayed / created_count / replayed_count：由
      items 顺序派生（不是各自分组拼接）。
    """

    items: tuple[FinancialClaimBatchItem, ...]
    fingerprints: dict[UUID, str]

    @property
    def claim_ids(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items)

    @property
    def created(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items if not item.replayed)

    @property
    def replayed(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items if item.replayed)

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def replayed_count(self) -> int:
        return len(self.replayed)


@dataclass(frozen=True)
class _LoadedReferences:
    """加载并校验后的全部 Calculation / Evidence 引用（真实 PG 数据）。"""

    source_evidence_ids: dict[UUID, tuple[UUID, ...]]  # calc_id -> source evidence ids
    evidence: dict[UUID, EvidenceCardModel]  # card_id -> card（source + additional）
    calculation_fingerprints: dict[UUID, str]  # calc_id -> calculation_fingerprint


@dataclass(frozen=True)
class _DerivedFinancialClaim:
    """纯函数阶段派生的全部确定性值（fingerprint / links / 策略结果）。"""

    fingerprint: str
    question_sha256: str
    evidence_by_relation: dict[str, list[UUID]]  # relation -> sorted evidence ids
    calculations_by_relation: dict[str, list[UUID]]  # relation -> sorted calc ids


def _assign_relation(auto: dict[UUID, str], card_id: UUID, relation: str) -> None:
    """把 Evidence 归入 relation；已有不同 relation → FinancialClaimRelationConflict。

    - 同一 relation 重复（多个 Calculations 共享同一 Evidence）→ 幂等去重；
    - 不同 relation 冲突 → 抛错（**不静默选一个**）。
    """
    current = auto.get(card_id)
    if current is None:
        auto[card_id] = relation
    elif current != relation:
        raise FinancialClaimRelationConflict()


class FinancialClaimService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_claim(self, draft: FinancialClaimDraft) -> FinancialClaimResult:
        """登记一条引用已登记 Calculations 的 Financial Claim（0 partial write）。"""
        batch = await self.create_claim_batch([draft])
        claim_id = batch.claim_ids[0]
        return FinancialClaimResult(
            claim_id=claim_id,
            claim_fingerprint=batch.fingerprints[claim_id],
            replayed=claim_id in batch.replayed,
        )

    async def create_claim_batch(
        self, drafts: list[FinancialClaimDraft]
    ) -> FinancialClaimBatchResult:
        """把 1..MAX_FINANCIAL_CLAIMS_PER_BATCH 条 Financial Claim 原子登记。

        两步提交结构（镜像 ClaimService.create_claim_batch）：
        1. **all-drafts-validate-first**——开事务前，对全部 drafts 加载引用并完成
           派生（自动展开 / relation semantics / critical policy / fingerprint）；
           任何一条失败 → 整批拒绝（0 写），**不允许 candidate 1 创建、
           candidate 2 才失败**。
        2. **单 transaction**——逐个 create_or_get + bulk insert links / replay
           校验；任一 SQLAlchemyError / FinancialClaimIntegrityError → 整批回滚，
           不留下半批 Claim（禁止 compensating delete）。
        items 按 input drafts 顺序返回（ordinal 一一对应）。
        """
        if not isinstance(drafts, list) or not (1 <= len(drafts) <= MAX_FINANCIAL_CLAIMS_PER_BATCH):
            raise FinancialClaimDraftError(f"drafts 必须在 1..{MAX_FINANCIAL_CLAIMS_PER_BATCH} 条")

        # 1. 短 DB session：一次性加载并校验全部 drafts 的 Calculation/Evidence 引用。
        async with self._sessionmaker() as session:
            loaded_list = [await self._load_validate_session(session, draft) for draft in drafts]

        # 2. 全部 drafts 先完成派生（任何一条失败 → 整批拒绝，0 写）。
        derived_list = [
            self._derive(draft, loaded) for draft, loaded in zip(drafts, loaded_list, strict=True)
        ]

        # 3. 单 transaction：逐个 create_or_get + links / replay。
        #    items 按 prepared（== input drafts）顺序收集，绝不按 created/replayed
        #    分组重排——items[i] 永远对应 drafts[i]。
        fingerprints: dict[UUID, str] = {}
        items: list[FinancialClaimBatchItem] = []
        async with self._sessionmaker() as session:
            try:
                repo = ClaimRepository(session)
                link_repo = ClaimEvidenceLinkRepository(session)
                calc_link_repo = ClaimFinancialCalculationLinkRepository(session)
                for ordinal, (draft, derived) in enumerate(
                    zip(drafts, derived_list, strict=True), start=1
                ):
                    existing = await repo.get_by_fingerprint(derived.fingerprint)
                    if existing is not None:
                        await self._verify_replay(session, existing, draft)
                        fingerprints[existing.claim_id] = derived.fingerprint
                        items.append(
                            FinancialClaimBatchItem(
                                ordinal=ordinal,
                                claim_id=existing.claim_id,
                                replayed=True,
                            )
                        )
                        continue

                    claim = ClaimModel(claim_id=uuid.uuid4(), **self._claim_kwargs(draft, derived))
                    persisted, created = await repo.create_or_get(claim)
                    if not created:
                        # 并发输家：复用既有 Claim（replay 校验后返回）。
                        await self._verify_replay(session, persisted, draft)
                        fingerprints[persisted.claim_id] = derived.fingerprint
                        items.append(
                            FinancialClaimBatchItem(
                                ordinal=ordinal,
                                claim_id=persisted.claim_id,
                                replayed=True,
                            )
                        )
                        continue

                    await link_repo.bulk_insert(self._evidence_links(persisted.claim_id, derived))
                    await calc_link_repo.bulk_insert(
                        self._calculation_links(persisted.claim_id, derived)
                    )
                    fingerprints[persisted.claim_id] = derived.fingerprint
                    items.append(
                        FinancialClaimBatchItem(
                            ordinal=ordinal,
                            claim_id=persisted.claim_id,
                            replayed=False,
                        )
                    )
                await session.commit()
                return FinancialClaimBatchResult(items=tuple(items), fingerprints=fingerprints)
            except FinancialClaimIntegrityError:
                # replay 校验发现既有 Claim 数据损坏 → 显式回滚本事务，然后抛出。
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise FinancialClaimPersistenceFailed() from exc

    # ------------------------------------------------------------------ 加载校验

    async def _load_validate_session(
        self,
        session: AsyncSession,
        draft: FinancialClaimDraft,
    ) -> _LoadedReferences:
        """加载并校验全部 Calculation / inputs / Observations / Evidence（J）。

        - 每个 Calculation：缺失 → FinancialClaimCalculationNotFound；company
          != draft → FinancialClaimCalculationMismatch；重放损坏 →
          FinancialClaimIntegrityError（包装自 FinancialCalculationError）；
        - 每个 input Observation：缺失 / company != draft → IntegrityError；
        - source Evidence + additional Evidence：缺失 / company != draft →
          FinancialClaimEvidenceCompanyMismatch。
        """
        calc_svc = FinancialCalculationService(self._sessionmaker)
        calc_ids = (
            draft.support_calculation_ids
            + draft.contradict_calculation_ids
            + draft.context_calculation_ids
        )
        fingerprints: dict[UUID, str] = {}
        for calc_id in calc_ids:
            try:
                calc = await calc_svc.verify_calculation_integrity(session, calc_id)
            except FinancialCalculationError as exc:
                raise FinancialClaimIntegrityError() from exc
            if calc is None:
                raise FinancialClaimCalculationNotFound()
            if calc.company_id != draft.company_id:
                raise FinancialClaimCalculationMismatch()
            fingerprints[calc_id] = calc.calculation_fingerprint

        # 加载 inputs → Observations → source evidence ids（按 calc 分组，canonical 排序）。
        input_repo = FinancialCalculationInputRepository(session)
        obs_repo = FinancialMetricObservationRepository(session)
        source_evidence_ids: dict[UUID, tuple[UUID, ...]] = {}
        all_source_ids: set[UUID] = set()
        for calc_id in calc_ids:
            rows = await input_repo.get_by_calculation_id(calc_id)
            per_calc: set[UUID] = set()
            for row in rows:
                obs = await obs_repo.get_by_id(row.metric_observation_id)
                if obs is None or obs.company_id != draft.company_id:
                    raise FinancialClaimIntegrityError(
                        "financial claim calculation input observation corrupted"
                    )
                per_calc.add(obs.source_evidence_card_id)
            source_evidence_ids[calc_id] = tuple(sorted(per_calc, key=str))
            all_source_ids |= per_calc

        evidence = await self._load_evidence_cards(session, all_source_ids, draft.company_id)

        additional_ids = (
            set(draft.additional_support_evidence_ids)
            | set(draft.additional_contradict_evidence_ids)
            | set(draft.additional_context_evidence_ids)
        )
        if additional_ids:
            evidence.update(
                await self._load_evidence_cards(session, additional_ids, draft.company_id)
            )

        return _LoadedReferences(
            source_evidence_ids=source_evidence_ids,
            evidence=evidence,
            calculation_fingerprints=fingerprints,
        )

    @staticmethod
    async def _load_evidence_cards(
        session: AsyncSession,
        card_ids: set[UUID],
        company_id: UUID,
    ) -> dict[UUID, EvidenceCardModel]:
        """从真实 PG 加载 EvidenceCards；缺失 / 跨公司 → FinancialClaimEvidenceCompanyMismatch。"""
        if not card_ids:
            return {}
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
        )
        rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(card_ids):
            raise FinancialClaimEvidenceCompanyMismatch()
        for card in by_id.values():
            if card.company_id != company_id:
                raise FinancialClaimEvidenceCompanyMismatch()
        return by_id

    # ------------------------------------------------------------------ 纯函数派生

    def _derive(
        self,
        draft: FinancialClaimDraft,
        loaded: _LoadedReferences,
    ) -> _DerivedFinancialClaim:
        """纯函数派生：自动展开 → relation semantics → critical policy → fingerprint。

        按 `draft.claim_schema_version` 分支：
        - v3（默认）：automatic source Evidence 一律 relation=context（Calculation
          承担 supports/contradicts/context 语义）；critical 用 deterministic
          source policy（任一 support Calculation 的 source Evidence eligible，或
          additional_support_evidence_ids 中存在 eligible）。
        - v2（legacy replay）：沿用原 relation propagation（calculation relation →
          source Evidence 同 relation）+ 旧 critical policy（最终 supports links
          中任一 eligible）。
        """
        auto: dict[UUID, str] = {}
        calc_ids_by_relation = {
            "supports": draft.support_calculation_ids,
            "contradicts": draft.contradict_calculation_ids,
            "context": draft.context_calculation_ids,
        }
        if draft.claim_schema_version == FINANCIAL_CLAIM_SCHEMA_VERSION_V2:
            # v2：legacy relation propagation（calculation relation → source
            # Evidence 同 relation），保持既有 v2 Claim 的 replay 语义。
            for relation, calc_ids in calc_ids_by_relation.items():
                for calc_id in calc_ids:
                    for card_id in loaded.source_evidence_ids[calc_id]:
                        _assign_relation(auto, card_id, relation)
        else:
            # v3：Calculation 承担 supports/contradicts/context 的语义关系；
            # automatic source Evidence 展开到 ClaimEvidenceLinks 时**一律
            # relation=context**（不是 propagation）。原始 metric Evidence 通常
            # 不能单独证明 derived financial conclusion。
            for calc_ids in calc_ids_by_relation.values():
                for calc_id in calc_ids:
                    for card_id in loaded.source_evidence_ids[calc_id]:
                        _assign_relation(auto, card_id, "context")

        # additional Evidence（管理层解释 / 业务事件 / 风险说明）保持调用方指定的
        # supports/contradicts/context（v2/v3 一致）。
        additional_by_relation = {
            "supports": draft.additional_support_evidence_ids,
            "contradicts": draft.additional_contradict_evidence_ids,
            "context": draft.additional_context_evidence_ids,
        }
        for relation, card_ids in additional_by_relation.items():
            for card_id in card_ids:
                _assign_relation(auto, card_id, relation)

        evidence_by_relation: dict[str, list[UUID]] = {
            relation: sorted(
                (card_id for card_id, rel in auto.items() if rel == relation),
                key=str,
            )
            for relation in _RELATIONS
        }

        # critical policy（schema 版本化）。
        if draft.importance == FinancialClaimImportance.CRITICAL:
            if draft.claim_schema_version == FINANCIAL_CLAIM_SCHEMA_VERSION_V2:
                # v2：沿用旧 policy——最终 supports evidence links（自动展开 +
                # additional 合并）中任一 eligible。
                supports_cards = [
                    loaded.evidence[card_id] for card_id in evidence_by_relation["supports"]
                ]
                if not any(card.critical_claim_eligible_snapshot for card in supports_cards):
                    raise FinancialClaimCriticalEvidenceInsufficient()
            else:
                # v3：不再依赖 ClaimEvidenceLink.supports 中的 automatic source
                # Evidence（现在都是 context）。deterministic source policy——至少
                # 满足其一：① 任一 support Calculation 展开得到的 source Evidence
                # 中存在 critical_claim_eligible_snapshot=true；②
                # additional_support_evidence_ids 中存在该属性。**Calculation 本身
                # 绝不提升 authority**。
                eligible = any(
                    loaded.evidence[card_id].critical_claim_eligible_snapshot
                    for calc_id in draft.support_calculation_ids
                    for card_id in loaded.source_evidence_ids[calc_id]
                ) or any(
                    loaded.evidence[card_id].critical_claim_eligible_snapshot
                    for card_id in draft.additional_support_evidence_ids
                )
                if not eligible:
                    raise FinancialClaimCriticalEvidenceInsufficient()

        # calculation lists（按 relation 分组，canonical 排序）。
        calculations_by_relation: dict[str, list[UUID]] = {
            relation: sorted(calc_ids, key=str)
            for relation, calc_ids in calc_ids_by_relation.items()
        }

        # fingerprint：v3 = claim_schema_version + claim semantic fields + evidence
        # links + calculation links/fingerprints（v2/v3 不 collision）。
        fingerprint = compute_financial_claim_fingerprint(
            claim_schema_version=draft.claim_schema_version,
            company_id=draft.company_id,
            research_question=draft.research_question,
            statement=draft.statement,
            claim_kind=draft.claim_kind.value,
            confidence=draft.confidence.value,
            importance=draft.importance.value,
            analyst_name=draft.analyst_name,
            analyst_version=draft.analyst_version,
            analyst_model_id=draft.analyst_model_id,
            supports_evidence=evidence_by_relation["supports"],
            contradicts_evidence=evidence_by_relation["contradicts"],
            context_evidence=evidence_by_relation["context"],
            supports_calculations=self._calculation_entries(
                calculations_by_relation["supports"], loaded.calculation_fingerprints
            ),
            contradicts_calculations=self._calculation_entries(
                calculations_by_relation["contradicts"], loaded.calculation_fingerprints
            ),
            context_calculations=self._calculation_entries(
                calculations_by_relation["context"], loaded.calculation_fingerprints
            ),
        )
        return _DerivedFinancialClaim(
            fingerprint=fingerprint,
            question_sha256=compute_research_question_sha256(draft.research_question),
            evidence_by_relation=evidence_by_relation,
            calculations_by_relation=calculations_by_relation,
        )

    @staticmethod
    def _calculation_entries(
        calc_ids: list[UUID],
        fingerprints: dict[UUID, str],
    ) -> list[dict]:
        return [
            {"calculation_id": str(cid), "calculation_fingerprint": fingerprints[cid]}
            for cid in calc_ids
        ]

    # ------------------------------------------------------------------ 持久化

    @staticmethod
    def _claim_kwargs(draft: FinancialClaimDraft, derived: _DerivedFinancialClaim) -> dict:
        return {
            "company_id": draft.company_id,
            "research_question": draft.research_question,
            "research_question_sha256": derived.question_sha256,
            "statement": draft.statement,
            "analysis_domain": ClaimAnalysisDomain.FINANCIAL.value,
            "claim_kind": draft.claim_kind.value,
            "confidence": draft.confidence.value,
            "importance": draft.importance.value,
            "analyst_name": draft.analyst_name,
            "analyst_version": draft.analyst_version,
            "analyst_model_id": draft.analyst_model_id,
            "claim_schema_version": draft.claim_schema_version,
            "claim_fingerprint": derived.fingerprint,
        }

    @staticmethod
    def _evidence_links(
        claim_id: UUID, derived: _DerivedFinancialClaim
    ) -> list[ClaimEvidenceLinkModel]:
        links: list[ClaimEvidenceLinkModel] = []
        for relation in _RELATIONS:
            for card_id in derived.evidence_by_relation[relation]:
                links.append(
                    ClaimEvidenceLinkModel(
                        claim_id=claim_id,
                        evidence_card_id=card_id,
                        relation=relation,
                    )
                )
        return links

    @staticmethod
    def _calculation_links(
        claim_id: UUID,
        derived: _DerivedFinancialClaim,
    ) -> list[ClaimFinancialCalculationLinkModel]:
        links: list[ClaimFinancialCalculationLinkModel] = []
        for relation in _RELATIONS:
            for calc_id in derived.calculations_by_relation[relation]:
                links.append(
                    ClaimFinancialCalculationLinkModel(
                        claim_id=claim_id,
                        calculation_id=calc_id,
                        relation=relation,
                    )
                )
        return links

    # ------------------------------------------------------------------ replay

    async def _verify_replay(
        self,
        session: AsyncSession,
        existing: ClaimModel,
        draft: FinancialClaimDraft,
    ) -> None:
        """已有 fingerprint 的 Financial Claim replay 完整性校验（M）。

        重新加载 Claim / evidence links / calculation links / Calculations /
        inputs / Observations / EvidenceCards，重新执行 Calculation integrity、
        自动 Evidence expansion、relation semantics / critical policy / relation
        conflict、按 draft.claim_schema_version 版本化的 fingerprint，逐项核实。
        任一损坏 → FinancialClaimIntegrityError，**不自动 repair**。
        """
        loaded = await self._load_validate_session(session, draft)
        try:
            derived = self._derive(draft, loaded)
        except (
            FinancialClaimCriticalEvidenceInsufficient,
            FinancialClaimRelationConflict,
        ) as exc:
            raise FinancialClaimIntegrityError(
                "financial claim replay integrity check failed on policy/relation"
            ) from exc

        ev_links = await ClaimEvidenceLinkRepository(session).list_by_claim(existing.claim_id)
        actual_ev = {
            relation: sorted(
                (link.evidence_card_id for link in ev_links if link.relation == relation),
                key=str,
            )
            for relation in _RELATIONS
        }
        for relation in _RELATIONS:
            if actual_ev[relation] != derived.evidence_by_relation[relation]:
                raise FinancialClaimIntegrityError(
                    f"financial claim replay integrity check failed on links[{relation}]"
                )

        calc_links = await ClaimFinancialCalculationLinkRepository(session).list_by_claim(
            existing.claim_id
        )
        actual_calc = {
            relation: sorted(
                (link.calculation_id for link in calc_links if link.relation == relation),
                key=str,
            )
            for relation in _RELATIONS
        }
        for relation in _RELATIONS:
            if actual_calc[relation] != derived.calculations_by_relation[relation]:
                raise FinancialClaimIntegrityError(
                    "financial claim replay integrity check failed on"
                    f" calculation links[{relation}]"
                )

        pairs = (
            ("company_id", existing.company_id, draft.company_id),
            ("research_question", existing.research_question, draft.research_question),
            (
                "research_question_sha256",
                existing.research_question_sha256,
                derived.question_sha256,
            ),
            ("statement", existing.statement, draft.statement),
            ("analysis_domain", existing.analysis_domain, ClaimAnalysisDomain.FINANCIAL.value),
            ("claim_kind", existing.claim_kind, draft.claim_kind.value),
            ("confidence", existing.confidence, draft.confidence.value),
            ("importance", existing.importance, draft.importance.value),
            ("analyst_name", existing.analyst_name, draft.analyst_name),
            ("analyst_version", existing.analyst_version, draft.analyst_version),
            ("analyst_model_id", existing.analyst_model_id, draft.analyst_model_id),
            (
                "claim_schema_version",
                existing.claim_schema_version,
                draft.claim_schema_version,
            ),
            ("claim_fingerprint", existing.claim_fingerprint, derived.fingerprint),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise FinancialClaimIntegrityError(
                    f"financial claim replay integrity check failed on {name}"
                )
