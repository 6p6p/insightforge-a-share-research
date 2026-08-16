"""Structured claim synthesis analysis service (stage 4D.1B).

流程（两步提交，镜像 SynthesisService / MacroAnalysisService）：
1. 防御性 request 校验（构造已校验，服务层再兜底）；
2. 短 DB session：调 **SynthesisService.verify_synthesis_integrity**（read-side
   公共 API，**不复制** SynthesisRun replay 规则）——重新加载 run + input links
   + 逐 claim 经 ClaimIntegrityGateway 完整校验（domain dispatch）+ 以 run 字段
   为预期重跑 company / research-question / temporal / cutoff 政策 + 重算
   synthesis_fingerprint 与 persisted 对比；缺失 → SynthesisAnalysisRunNotFound
   （**不调用 LLM**），损坏 → 稳定 integrity 错误，不自动 repair。再取 company
   name；
3. 关闭 DB session（**LLM 调用期间不持有 DB transaction / connection**）；
4. 纯函数构造 deterministic Claim Pack（C alias 按 analysis_domain + claim_id
   canonical 排序，**LLM 永不看 UUID**）；
5. 调 SynthesisAnalysisModel.analyze → SynthesisAnalysisOutput（provider 失败 →
   ModelUnavailable；输出无法解析 → MalformedOutput）；
6. strict validation（validate_synthesis_output：全部 C refs 已知 + no-cherry-
   picking 硬边界——claim_roles 恰好覆盖每条 input Claim 一次）；
7. compute_synthesis_result_fingerprint（result_schema_version + run fingerprint +
   analyst identity + output，SHA-256）；
8. 短 DB transaction：create_or_get result（ON CONFLICT(result_fingerprint)，无
   进程锁）→ 命中时完整 replay 校验（逐字段核实），任何损坏 →
   SynthesisIntegrityError，**不自动 repair**；SQLAlchemyError → rollback +
   SynthesisAnalysisPersistenceFailed；
9. 返回 SynthesisAnalysisResult（不含任何正文文本）。

**不创建 Report / DraftSection / Audit**；不接 LangGraph；不调用 Retrieval /
Chroma / RawArtifact / tools / web search；不复制 Evidence / Calculation /
Transmission / Comparison 的 ID 到 result 表。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.synthesis.contracts import (
    SYNTHESIS_ANALYST_FOCUS,
    SYNTHESIS_ANALYST_NAME,
    SYNTHESIS_ANALYST_VERSION,
    SYNTHESIS_RESULT_SCHEMA_VERSION,
    SynthesisAnalysisContext,
    SynthesisAnalysisOutput,
    SynthesisAnalysisRequest,
    SynthesisAnalysisResult,
    VerifiedSynthesisResult,
    compute_synthesis_result_fingerprint,
    validate_synthesis_output,
)
from app.analysis.synthesis.errors import (
    SynthesisAnalysisError,
    SynthesisAnalysisInputError,
    SynthesisAnalysisMalformedOutput,
    SynthesisAnalysisModelUnavailable,
    SynthesisAnalysisPersistenceFailed,
    SynthesisAnalysisResultNotFound,
    SynthesisAnalysisRunNotFound,
    SynthesisResultIntegrityError,
)
from app.analysis.synthesis.model import SynthesisAnalysisModel
from app.analysis.synthesis.packs import SynthesisClaimPack, build_claim_pack
from app.core.logging import get_logger
from app.db.models.claim_synthesis_result import ClaimSynthesisResultModel
from app.db.models.company import CompanyModel
from app.repositories.claim_synthesis_result_repository import (
    ClaimSynthesisResultRepository,
)
from app.synthesis.contracts import VerifiedSynthesisClaim, VerifiedSynthesisRun
from app.synthesis.errors import SynthesisIntegrityError, SynthesisRunNotFound
from app.synthesis.service import SynthesisService


@dataclass(frozen=True)
class _LoadedSynthesisInput:
    """短 DB session 的加载产物（session 关闭后不再持有连接）。"""

    synthesis_id: UUID
    research_question: str
    analysis_as_of: date
    company_name: str
    synthesis_fingerprint: str
    claims: list[VerifiedSynthesisClaim]


class SynthesisAnalysisService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        model: SynthesisAnalysisModel | None = None,
    ) -> None:
        """model 可选：`analyze` 需要模型，`verify_result_integrity` 不需要
        （Stage 5A ReportOutline 是 0 LLM 派生，只消费已验证结果）。
        """
        self._sessionmaker = sessionmaker
        self._model = model
        # read-side integrity（Gate 0）委托 SynthesisService 公共 API，不复制规则。
        self._logger = get_logger("app.synthesis.analysis")
        self._synthesis = SynthesisService(sessionmaker)

    async def analyze(self, request: SynthesisAnalysisRequest) -> SynthesisAnalysisResult:
        if self._model is None:
            raise SynthesisAnalysisModelUnavailable()
        # 1. 防御性 request 校验（构造已校验，服务层再兜底）。
        self._check_request(request)

        # 2. 短 DB session：加载 run + input links + gateway 校验 claims + company。
        loaded = await self._load_synthesis(request)

        # 3-4. 关闭 session；纯函数构造 deterministic Claim Pack。
        claim_pack = build_claim_pack(
            research_question=loaded.research_question,
            analysis_as_of=loaded.analysis_as_of,
            company_name=loaded.company_name,
            claims=loaded.claims,
        )
        context = SynthesisAnalysisContext(
            research_question=loaded.research_question,
            analysis_as_of=loaded.analysis_as_of,
            strategy=SYNTHESIS_ANALYST_FOCUS,
        )

        # 5-6. 调模型（结构化综合）+ strict validation（no-cherry-picking
        # 硬边界；LLM 调用期间不持有 DB transaction）。有界重试：生产实测
        # DeepSeek 瞬时 5xx/超时/偶发违规输出——重试 2 次（校验违规与模型
        # 不可用都重试）仍失败才抛（orchestration 可 retry；不写任何行）。
        claim_refs = list(claim_pack.alias_map().keys())
        output = None
        for attempt in range(3):
            try:
                output = await self._call_model(context, claim_pack)
                validate_synthesis_output(output, claim_refs)
                break
            except Exception as exc:  # noqa: BLE001 - 模型/校验瞬时失败
                if attempt < 2:
                    self._logger.warning(
                        "synthesis_model_retry",
                        attempt=attempt,
                        error_type=type(exc).__name__,
                    )
                    continue
                raise SynthesisAnalysisModelUnavailable() from exc

        # 7. 确定性指纹（result_schema_version + run fingerprint + analyst + output）。
        fingerprint = compute_synthesis_result_fingerprint(
            result_schema_version=SYNTHESIS_RESULT_SCHEMA_VERSION,
            synthesis_fingerprint=loaded.synthesis_fingerprint,
            analyst_name=SYNTHESIS_ANALYST_NAME,
            analyst_version=SYNTHESIS_ANALYST_VERSION,
            analyst_model_id=self._model.model_id,
            output=output,
        )

        # 8. 短 DB transaction：create_or_get result（原子）+ replay 校验。
        result_model = self._result_model(loaded, output, fingerprint)
        async with self._sessionmaker() as session:
            try:
                row, was_created = await ClaimSynthesisResultRepository(session).create_or_get(
                    result_model
                )
                if not was_created:
                    # 并发输家 / 已存在结果：完整 replay 校验（不写任何行）。
                    await self._verify_replay(session, row, result_model)
                await session.commit()
            except SynthesisIntegrityError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise SynthesisAnalysisPersistenceFailed() from exc

        # 9. 结果摘要（不含任何正文文本 / themes / conflicts）。
        return SynthesisAnalysisResult(
            synthesis_result_id=row.synthesis_result_id,
            synthesis_id=row.synthesis_id,
            result_fingerprint=row.result_fingerprint,
            replayed=not was_created,
            claim_count=len(loaded.claims),
        )

    # ------------------------------------------------- public read-side verify (Stage 5A)

    async def verify_result_integrity(self, synthesis_result_id: UUID) -> VerifiedSynthesisResult:
        """公共 read-side 完整性校验（Stage 5A：ReportOutline 的 verified 输入）。

        短 DB session 加载 result 行 + `verify_synthesis_integrity`（read-side
        公共 API，**不复制** SynthesisRun replay 规则）→ 关闭 session → 纯函数
        路径：result schema / analyst 身份 / payload 解析 / resolved claim IDs
        全属 exact input set（复用 `validate_synthesis_output` 的 no-cherry-
        picking 边界）/ 重算 result_fingerprint。任一损坏 →
        `SynthesisResultIntegrityError`，**不自动 repair**；result 缺失 →
        `SynthesisAnalysisResultNotFound`。**不调用模型**（0 LLM）。
        """
        async with self._sessionmaker() as session:
            row = await ClaimSynthesisResultRepository(session).get_by_id(synthesis_result_id)
            if row is None:
                raise SynthesisAnalysisResultNotFound()
            try:
                verified_run = await self._synthesis.verify_synthesis_integrity(
                    session, row.synthesis_id
                )
            except SynthesisRunNotFound:
                raise SynthesisAnalysisRunNotFound() from None
            company = await session.get(CompanyModel, verified_run.company_id)
            if company is None:
                raise SynthesisIntegrityError("synthesis result company missing")
            company_name = company.short_name or company.official_name

        # ---- session 已关闭；以下纯函数路径 ----
        if row.result_schema_version != SYNTHESIS_RESULT_SCHEMA_VERSION:
            raise SynthesisResultIntegrityError("synthesis result schema version mismatch")
        if (
            row.analyst_name != SYNTHESIS_ANALYST_NAME
            or row.analyst_version != SYNTHESIS_ANALYST_VERSION
        ):
            raise SynthesisResultIntegrityError("synthesis result analyst identity mismatch")

        try:
            output = SynthesisAnalysisOutput.model_validate(
                {
                    "summary": row.summary,
                    "themes": row.themes,
                    "claim_roles": row.claim_roles,
                    "duplicates": row.duplicates,
                    "conflicts": row.conflicts,
                    "evidence_gaps": row.evidence_gaps,
                }
            )
        except ValidationError:
            raise SynthesisResultIntegrityError(
                "synthesis result payload failed schema validation"
            ) from None

        claim_pack = build_claim_pack(
            research_question=verified_run.research_question,
            analysis_as_of=verified_run.analysis_as_of,
            company_name=company_name,
            claims=verified_run.verified_claims,
        )
        alias_map = claim_pack.alias_map()
        try:
            validate_synthesis_output(output, list(alias_map.keys()))
        except SynthesisAnalysisError:
            # 全部 C refs 已知 + claim_roles 恰好覆盖每条 input Claim。
            raise SynthesisResultIntegrityError(
                "synthesis result refs no longer cover the exact input claim set"
            ) from None

        recomputed = compute_synthesis_result_fingerprint(
            result_schema_version=row.result_schema_version,
            synthesis_fingerprint=verified_run.synthesis_fingerprint,
            analyst_name=row.analyst_name,
            analyst_version=row.analyst_version,
            analyst_model_id=row.analyst_model_id,
            output=output,
        )
        if recomputed != row.result_fingerprint:
            raise SynthesisResultIntegrityError("synthesis result fingerprint mismatch")

        return VerifiedSynthesisResult(
            synthesis_result_id=row.synthesis_result_id,
            synthesis_id=row.synthesis_id,
            company_id=verified_run.company_id,
            research_question=verified_run.research_question,
            research_question_sha256=verified_run.research_question_sha256,
            analysis_as_of=verified_run.analysis_as_of,
            synthesis_fingerprint=verified_run.synthesis_fingerprint,
            result_fingerprint=row.result_fingerprint,
            input_claim_ids=tuple(claim.claim_id for claim in verified_run.verified_claims),
            alias_map=alias_map,
            output=output,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _check_request(request: SynthesisAnalysisRequest) -> None:
        # 构造时已做校验；此处仅防御性确认关键不变量（避免绕过 dataclass）。
        if isinstance(request.synthesis_id, bool) or not isinstance(request.synthesis_id, UUID):
            raise SynthesisAnalysisInputError("synthesis_id 必须是 UUID")

    async def _load_synthesis(self, request: SynthesisAnalysisRequest) -> _LoadedSynthesisInput:
        """短 DB session：SynthesisService.verify_synthesis_integrity（read-side 校验）。

        只消费 VerifiedSynthesisRun（**不复制** SynthesisRun replay 规则）：
        重新加载 run + input links → 逐 claim gateway 校验 → 以 run 字段为预期
        重跑 company / research-question / temporal / cutoff → 重算 fingerprint
        对比。缺失 → SynthesisAnalysisRunNotFound；已登记 run 被篡改 → 稳定
        integrity 错误，**不自动 repair**。
        """
        async with self._sessionmaker() as session:
            try:
                verified_run: VerifiedSynthesisRun = (
                    await self._synthesis.verify_synthesis_integrity(session, request.synthesis_id)
                )
            except SynthesisRunNotFound:
                raise SynthesisAnalysisRunNotFound() from None
            company = await session.get(CompanyModel, verified_run.company_id)
            if company is None:
                raise SynthesisIntegrityError("synthesis run company missing")
            company_name = company.short_name or company.official_name
        return _LoadedSynthesisInput(
            synthesis_id=verified_run.synthesis_id,
            research_question=verified_run.research_question,
            analysis_as_of=verified_run.analysis_as_of,
            company_name=company_name,
            synthesis_fingerprint=verified_run.synthesis_fingerprint,
            claims=verified_run.verified_claims,
        )

    async def _call_model(
        self,
        context: SynthesisAnalysisContext,
        claim_pack: SynthesisClaimPack,
    ) -> SynthesisAnalysisOutput:
        """调用模型并归一到 SynthesisAnalysisOutput（防御性 double-check）。

        模型层负责解析；这里再对返回结果做一次 schema 校验（provider 可能
        返回 raw dict / 已构造对象），ValidationError → MalformedOutput。
        """
        raw = await self._model.analyze(context, claim_pack)
        if isinstance(raw, SynthesisAnalysisOutput):
            return raw
        try:
            return SynthesisAnalysisOutput.model_validate(raw)
        except ValidationError as exc:
            raise SynthesisAnalysisMalformedOutput() from exc

    def _result_model(
        self,
        loaded: _LoadedSynthesisInput,
        output: SynthesisAnalysisOutput,
        fingerprint: str,
    ) -> ClaimSynthesisResultModel:
        """把验证过的输出构造成不可变 result 行（JSONB 用 canonical dict 投影）。"""
        return ClaimSynthesisResultModel(
            synthesis_result_id=uuid.uuid4(),
            synthesis_id=loaded.synthesis_id,
            result_schema_version=SYNTHESIS_RESULT_SCHEMA_VERSION,
            result_fingerprint=fingerprint,
            themes=[theme.model_dump(mode="json") for theme in output.themes],
            claim_roles=[assignment.model_dump(mode="json") for assignment in output.claim_roles],
            duplicates=[d.model_dump(mode="json") for d in output.duplicates],
            conflicts=[c.model_dump(mode="json") for c in output.conflicts],
            evidence_gaps=[gap.model_dump(mode="json") for gap in output.evidence_gaps],
            summary=output.summary,
            analyst_name=SYNTHESIS_ANALYST_NAME,
            analyst_version=SYNTHESIS_ANALYST_VERSION,
            analyst_model_id=self._model.model_id,
        )

    @staticmethod
    async def _verify_replay(
        session,
        row: ClaimSynthesisResultModel,
        expected: ClaimSynthesisResultModel,
    ) -> None:
        """replay 完整性校验（同 run + 同 analyst + 同输出 = 同 fingerprint）。

        逐字段核实既有行与本次派生一致；任何不一致 → SynthesisIntegrityError，
        **不自动 repair**（同 fingerprint 冲突行被篡改 → 拒绝）。
        """
        checks = [
            (row.synthesis_id, expected.synthesis_id, "synthesis_id"),
            (
                row.result_schema_version,
                expected.result_schema_version,
                "result_schema_version",
            ),
            (row.result_fingerprint, expected.result_fingerprint, "result_fingerprint"),
            (row.themes, expected.themes, "themes"),
            (row.claim_roles, expected.claim_roles, "claim_roles"),
            (row.duplicates, expected.duplicates, "duplicates"),
            (row.conflicts, expected.conflicts, "conflicts"),
            (row.evidence_gaps, expected.evidence_gaps, "evidence_gaps"),
            (row.summary, expected.summary, "summary"),
            (row.analyst_name, expected.analyst_name, "analyst_name"),
            (row.analyst_version, expected.analyst_version, "analyst_version"),
            (row.analyst_model_id, expected.analyst_model_id, "analyst_model_id"),
        ]
        for actual, want, field in checks:
            if actual != want:
                raise SynthesisIntegrityError(f"synthesis result {field} mismatch")
