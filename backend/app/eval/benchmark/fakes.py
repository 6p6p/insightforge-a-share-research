"""Offline deterministic model fakes for the benchmark experiment (stage 7B.1.4D).

`--fake` 模式的确定性模型集（0 真实 DeepSeek / 0 网络）：三路 variant 各一套
fake bundle，产出与 inputs 完全确定的结果——用于离线回归 / CI / 无 key 环境，
证明三路 pipeline 端到端可执行。

**自包含**：不 import `tests.*`（CLI 可能从任意 CWD 运行）；固定 decision 工厂
（financial / macro / valuation / audit pass / revision / draft）与 E2E 测试用
同一语义（relate_high valuation、pass audit 等），保证 benchmark 结果与 E2E 验收
一致。
"""

from __future__ import annotations

from app.analysis.claims.contracts import (
    MAX_CLAIMS_PER_DECISION,
    ClaimAnalysisDecision,
    ClaimAnalysisReason,
    ClaimCandidate,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.financial.contracts import (
    FinancialAnalysisDecision,
    FinancialClaimCandidate,
)
from app.analysis.financial.service import FinancialAnalysisService
from app.analysis.macro.contracts import MacroAnalysisDecision, MacroClaimCandidate
from app.analysis.macro.service import MacroAnalysisService
from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisTheme,
)
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.analysis.valuation.contracts import ValuationAnalysisDecision
from app.analysis.valuation.service import ValuationAnalysisService
from app.audit.contracts import AuditDecision
from app.audit.packs import AuditPack
from app.audit.service import ReportAuditService
from app.claims.financial_contracts import (
    FinancialClaimConfidence,
    FinancialClaimImportance,
)
from app.claims.macro_contracts import (
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)
from app.draft_section.contracts import ParagraphCandidate, WriterDecision
from app.draft_section.packs import SectionInputPack
from app.draft_section.service import DraftSectionService
from app.eval.contracts import EvalExecutionConfig
from app.eval.variants.insightforge_full.contracts import FullModelFactoryBundle
from app.eval.variants.multi_stage_no_audit.contracts import MultiStageModelFactoryBundle
from app.eval.variants.single_rag.contracts import (
    SingleRagAnswerModel,
    SingleRagModelClaim,
    SingleRagModelOutput,
)
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.llm.components import (
    COMPONENT_AUDIT,
    COMPONENT_CLAIM_ANALYSIS,
    COMPONENT_DRAFT_SECTION_WRITER,
    COMPONENT_EVAL_SINGLE_RAG_ANSWER,
    COMPONENT_EVIDENCE_EXTRACTION,
    COMPONENT_FINANCIAL_ANALYSIS,
    COMPONENT_MACRO_ANALYSIS,
    COMPONENT_RESEARCH_PLANNER,
    COMPONENT_REVISION_WRITER,
    COMPONENT_SYNTHESIS_ANALYSIS,
    COMPONENT_VALUATION_ANALYSIS,
)
from app.llm.instrumentation import (
    LlmCallOutcome,
    LlmCallUsageRecord,
    LlmUsageObserver,
    UsageStatus,
)
from app.report.check_service import ReportCheckService
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.research_backflow.service import ResearchBackflowService
from app.research_planning.contracts import (
    ResearchPlannerRequest,
    ResearchPlanPayload,
)
from app.review.service import ReviewActionService
from app.revision.packs import RevisionInputPack
from app.revision.service import RevisionService
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage5.dependencies import Stage5WorkflowDependencies
from app.synthesis.service import SynthesisService
from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
)

# 每条 fake LLM 调用报告的 token 数（确定性成本核算输入）。
_FAKE_INPUT_TOKENS = 20
_FAKE_OUTPUT_TOKENS = 20
_FAKE_DURATION_MS = 1


async def _record_usage(
    observer: LlmUsageObserver | None, component: str, *, provider: str, model_id: str
) -> None:
    if observer is None:
        return
    await observer.record(
        LlmCallUsageRecord(
            component_name=component,
            provider=provider,
            model_id=model_id,
            outcome=LlmCallOutcome.SUCCESS,
            duration_ms=_FAKE_DURATION_MS,
            usage_status=UsageStatus.REPORTED,
            input_tokens=_FAKE_INPUT_TOKENS,
            output_tokens=_FAKE_OUTPUT_TOKENS,
            total_tokens=_FAKE_INPUT_TOKENS + _FAKE_OUTPUT_TOKENS,
        )
    )


# ------------------------------------------------------------------ 确定性决策工厂


def _unique_quote(text: str, quote_len: int) -> str:
    """取 text 的唯一精确子串（quote resolver 要求出现恰好 1 次）。"""
    boundary = next((i for i in range(1, len(text)) if text[i] != text[i - 1]), 0)
    start = max(0, boundary - quote_len // 2)
    return text[start : start + quote_len]


def _unique_quote_with_marker(text: str, quote_len: int = 40) -> str:
    """quote 从段落唯一标记「第N段」开始（chunk 内唯一），无标记回退通用实现。"""
    marker = text.find("第")
    if marker >= 0:
        return text[marker : marker + quote_len]
    return _unique_quote(text, 20)


def _pass_decision(pack: AuditPack) -> AuditDecision:
    """全部 P refs + 0 issues（pass 语义；与 E2E `pass_decision` 同构）。"""
    return AuditDecision(
        reviewed_paragraph_refs=[paragraph.paragraph_ref for paragraph in pack.paragraphs],
        issues=[],
    )


def _valid_decision_for(pack: SectionInputPack) -> WriterDecision:
    """对该 pack 一定合法的 WriterDecision（与 tests 的 `valid_decision_for` 同构）。"""
    claim = pack.claims[0]
    evidence = next(item for item in pack.evidence if claim.alias in item.claim_aliases)
    paragraphs = [
        ParagraphCandidate(
            text=f"{claim.statement} {evidence.evidence_statement}",
            claim_refs=[claim.alias],
            evidence_refs=[evidence.alias],
        )
    ]
    for conflict in pack.conflicts:
        if conflict.claim_aliases:
            c_alias = conflict.claim_aliases[0]
            c = next(item for item in pack.claims if item.alias == c_alias)
            ev = next(item for item in pack.evidence if c_alias in item.claim_aliases)
            paragraphs.append(
                ParagraphCandidate(
                    text=f"{conflict.description} {c.statement} {ev.evidence_statement}",
                    claim_refs=[c_alias],
                    evidence_refs=[ev.alias],
                    conflict_refs=[conflict.alias],
                )
            )
    for gap in pack.gaps:
        if gap.claim_aliases:
            c_alias = gap.claim_aliases[0]
            c = next(item for item in pack.claims if item.alias == c_alias)
            ev = next(item for item in pack.evidence if c_alias in item.claim_aliases)
            paragraphs.append(
                ParagraphCandidate(
                    text=f"{gap.description} {c.statement} {ev.evidence_statement}",
                    claim_refs=[c_alias],
                    evidence_refs=[ev.alias],
                    gap_refs=[gap.alias],
                )
            )
    return WriterDecision(paragraphs=paragraphs)


def _revision_decision_for(pack: RevisionInputPack) -> WriterDecision:
    """对该 RevisionInputPack 一定合法的修订决策（与 E2E `revision_decision_for` 同构）。"""
    base = _valid_decision_for(pack.input_pack)
    marker = "修订版"
    if pack.revision_feedback:
        item = pack.revision_feedback[0]
        marker = f"修订版[{item.trigger_type}:{item.code}]"
    paragraphs = [
        ParagraphCandidate(
            text=f"{marker}。{p.text}",
            claim_refs=list(p.claim_refs),
            evidence_refs=list(p.evidence_refs),
            conflict_refs=list(p.conflict_refs),
            gap_refs=list(p.gap_refs),
        )
        for p in base.paragraphs
    ]
    return WriterDecision(paragraphs=paragraphs)


def _financial_decision() -> FinancialAnalysisDecision:
    return FinancialAnalysisDecision(
        relevant=True,
        claims=[
            FinancialClaimCandidate(
                statement="营业收入保持增长态势。",
                claim_kind=ClaimKind.INFERENCE,
                confidence=FinancialClaimConfidence.HIGH,
                importance=FinancialClaimImportance.NORMAL,
                support_calculation_refs=["C1"],
                contradict_calculation_refs=[],
                context_calculation_refs=[],
                additional_support_evidence_refs=[],
                additional_contradict_evidence_refs=[],
                additional_context_evidence_refs=[],
            )
        ],
    )


def _macro_decision() -> MacroAnalysisDecision:
    return MacroAnalysisDecision(
        relevant=True,
        claims=[
            MacroClaimCandidate(
                statement="利率上行或对公司融资成本形成压力。",
                claim_kind=ClaimKind.RISK,
                confidence=MacroClaimConfidence.MEDIUM,
                importance=MacroClaimImportance.NORMAL,
                channel_type=MacroChannelType.FINANCING,
                effect_direction=MacroEffectDirection.HEADWIND,
                impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
                time_alignment=MacroTimeAlignment.ALIGNED,
                macro_driver_refs=["M1"],
                company_exposure_refs=["E1"],
                observed_effect_refs=[],
                additional_support_evidence_refs=[],
                additional_contradict_evidence_refs=[],
                additional_context_evidence_refs=[],
            )
        ],
    )


def _valuation_decision() -> ValuationAnalysisDecision:
    """RELATIVE_HIGH：要求全部 support comparison 的 premium 为正（dataset 已保证）。"""
    return ValuationAnalysisDecision(
        relevant=True,
        assessment=ValuationClaimAssessment.RELATIVE_HIGH,
        confidence=ValuationClaimConfidence.HIGH,
        importance=ValuationClaimImportance.NORMAL,
        support_comparison_refs=["V1"],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=None,
    )


# ------------------------------------------------------------------ single_rag


class FakeSingleRagAnswerModel:
    """确定性 RAG 回答：第一条 context entry → 一条 claim（citation 闭合）。

    usage 记录绑定 **call-time** `usage_observer`（runner 线程注入），构造时
    observer 仅作 fallback。
    """

    def __init__(self, *, provider: str, model_id: str, observer: LlmUsageObserver | None) -> None:
        self.provider = provider
        self.model_id = model_id
        self._observer = observer
        self.calls = 0

    async def answer(self, research_question, context_entries, *, usage_observer=None):
        self.calls += 1
        await _record_usage(
            usage_observer if usage_observer is not None else self._observer,
            COMPONENT_EVAL_SINGLE_RAG_ANSWER,
            provider=self.provider,
            model_id=self.model_id,
        )
        claims = ()
        if context_entries:
            claims = (
                SingleRagModelClaim(
                    claim_id="C1",
                    statement="结论可追溯到给定检索上下文。",
                    citation_keys=(context_entries[0].key,),
                ),
            )
        return SingleRagModelOutput(
            final_text="基于检索上下文的结论：公司经营情况与检索材料一致。",
            claims=claims,
        )


def create_single_rag_fake_answer(
    config: EvalExecutionConfig, observer: LlmUsageObserver | None
) -> SingleRagAnswerModel:
    return FakeSingleRagAnswerModel(
        provider=config.model.provider,
        model_id=config.model.model_id,
        observer=observer,
    )


# ------------------------------------------------------------------ multi_stage / full


class _PlannerModel:
    """确定性 planner：每次 generate 返回固定 payload + 记录 usage。"""

    def __init__(self, payload, *, observer, provider, model_id) -> None:
        self._payload = payload
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls: list[ResearchPlannerRequest] = []

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def generate(self, request: ResearchPlannerRequest) -> ResearchPlanPayload:
        self.calls.append(request)
        await _record_usage(
            self._observer,
            COMPONENT_RESEARCH_PLANNER,
            provider=self._provider,
            model_id=self._model_id,
        )
        return self._payload


class _E2eEvidenceModel:
    """按真实 RetrievalHit.text 生成确定性 decision（quote 唯一可解析）+ usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def extract(self, research_question, retrieval_hit):
        self.calls += 1
        await _record_usage(
            self._observer,
            COMPONENT_EVIDENCE_EXTRACTION,
            provider=self._provider,
            model_id=self._model_id,
        )
        text_value = retrieval_hit.text
        if not any(text_value[i] != text_value[i - 1] for i in range(1, len(text_value))):
            return EvidenceExtractionDecision(
                relevant=False, items=[], reason_code=EvidenceExtractionReason.NOT_RELEVANT
            )
        return EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement="公司发布经营相关材料。",
                    evidence_type=EvidenceType.METRIC,
                    quote_text=_unique_quote_with_marker(text_value),
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )


class _E2eClaimModel:
    """Ref-aware claim fake：引用 evidence pack 全部 refs（<= 上限）+ usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, context, evidence_pack):
        self.calls += 1
        await _record_usage(
            self._observer,
            COMPONENT_CLAIM_ANALYSIS,
            provider=self._provider,
            model_id=self._model_id,
        )
        domain = context.analysis_domain.value
        kind = (
            ClaimKind.RISK
            if domain == "risk"
            else ClaimKind.INFERENCE
            if domain == "event"
            else ClaimKind.FACT
        )
        items = evidence_pack.items[:MAX_CLAIMS_PER_DECISION]
        if not items:
            return ClaimAnalysisDecision(
                relevant=False,
                claims=[],
                reason_code=ClaimAnalysisReason.INSUFFICIENT_EVIDENCE,
            )
        claims = [
            ClaimCandidate(
                statement=f"{domain} 域证据支持公司基本面结论。",
                claim_kind=kind,
                confidence=ClaimConfidence.HIGH,
                importance=ClaimImportance.NORMAL,
                support_refs=[item.evidence_ref for item in items],
                contradict_refs=[],
                context_refs=[],
            )
        ]
        return ClaimAnalysisDecision(relevant=True, claims=claims)


class _RecordingFixedModel:
    """包装固定 decision 的 fake（financial / macro / valuation 分析，usage 记录）。"""

    def __init__(self, decision, component: str, *, observer, provider, model_id) -> None:
        self._decision = decision
        self._component = component
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, *args, **kwargs):
        self.calls += 1
        await _record_usage(
            self._observer, self._component, provider=self._provider, model_id=self._model_id
        )
        return self._decision


class _E2eSynthesisModel:
    """确定性 synthesis fake：输出从 claim pack 的 C alias 派生 + usage。

    拆多个 theme（每个 <=5 refs）→ outline 每个 section 的 claim 集合受控，
    避免单 section 段落数超过 WriterDecision 上限（1..10）。
    """

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, context, claim_pack):
        self.calls += 1
        await _record_usage(
            self._observer,
            COMPONENT_SYNTHESIS_ANALYSIS,
            provider=self._provider,
            model_id=self._model_id,
        )
        refs = list(claim_pack.alias_map().keys())
        themes = [
            SynthesisTheme(
                title="经营质量综合评估" if i == 0 else f"证据主题 {i}",
                summary="各域证据指向一致。",
                claim_refs=refs[i : i + 5],
            )
            for i in range(0, len(refs), 5)
        ]
        return SynthesisAnalysisOutput(
            summary="综合判断：多维度证据一致支持公司基本面结论。",
            themes=themes,
            claim_roles=[
                SynthesisClaimRoleAssignment(
                    claim_ref=ref,
                    role=SynthesisClaimRole.SUPPORT,
                    rationale=f"支持 {ref}",
                )
                for ref in refs
            ],
            duplicates=[],
            conflicts=[],
            evidence_gaps=[],
        )


def _e2e_draft_decision_for(pack: SectionInputPack) -> WriterDecision:
    """确定性 draft decision：按 claim.statement 排序（跨 attempt 稳定），段落 <=10。"""
    paragraphs = []
    for claim in sorted(pack.claims, key=lambda item: item.statement)[:10]:
        evidence = next((item for item in pack.evidence if claim.alias in item.claim_aliases), None)
        if evidence is None:
            continue
        paragraphs.append(
            ParagraphCandidate(
                text=f"{claim.statement} {evidence.evidence_statement}",
                claim_refs=[claim.alias],
                evidence_refs=[evidence.alias],
            )
        )
    return WriterDecision(paragraphs=paragraphs)


class _E2eDraftModel:
    """确定性 draft fake：按语义字段排序的 decision（跨 attempt 稳定）+ usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def write(self, pack):
        self.calls += 1
        await _record_usage(
            self._observer,
            COMPONENT_DRAFT_SECTION_WRITER,
            provider=self._provider,
            model_id=self._model_id,
        )
        return _e2e_draft_decision_for(pack)


class _E2eAuditModel:
    """确定性 audit fake：decision_factory（sequenced）+ usage 记录。"""

    def __init__(self, decision_factory, *, observer, provider, model_id) -> None:
        self._decision_factory = decision_factory
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def audit(self, pack):
        self.calls += 1
        await _record_usage(
            self._observer, COMPONENT_AUDIT, provider=self._provider, model_id=self._model_id
        )
        return self._decision_factory(pack)


class _E2eRevisionModel:
    """确定性 revision fake：revision_decision_for + usage 记录。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def rewrite(self, pack):
        self.calls += 1
        await _record_usage(
            self._observer,
            COMPONENT_REVISION_WRITER,
            provider=self._provider,
            model_id=self._model_id,
        )
        return _revision_decision_for(pack)


class _SequencedFactories:
    """按调用次序返回不同 decision（最后重复）。"""

    def __init__(self, factories) -> None:
        self._factories = list(factories)
        self.calls = 0

    def __call__(self, pack):
        idx = min(self.calls, len(self._factories) - 1)
        self.calls += 1
        return self._factories[idx](pack)


def _pass_decision_factory():
    return _pass_decision


def _stage4_deps(sessionmaker, *, observer, provider, model_id) -> Stage4AnalysisDependencies:
    return Stage4AnalysisDependencies(
        sessionmaker=sessionmaker,
        claim_analysis_service=ClaimAnalysisService(
            sessionmaker, _E2eClaimModel(observer=observer, provider=provider, model_id=model_id)
        ),
        financial_analysis_service=FinancialAnalysisService(
            sessionmaker,
            _RecordingFixedModel(
                _financial_decision(),
                COMPONENT_FINANCIAL_ANALYSIS,
                observer=observer,
                provider=provider,
                model_id=model_id,
            ),
        ),
        macro_analysis_service=MacroAnalysisService(
            sessionmaker,
            _RecordingFixedModel(
                _macro_decision(),
                COMPONENT_MACRO_ANALYSIS,
                observer=observer,
                provider=provider,
                model_id=model_id,
            ),
        ),
        valuation_analysis_service=ValuationAnalysisService(
            sessionmaker,
            _RecordingFixedModel(
                _valuation_decision(),
                COMPONENT_VALUATION_ANALYSIS,
                observer=observer,
                provider=provider,
                model_id=model_id,
            ),
        ),
        synthesis_service=SynthesisService(sessionmaker),
        synthesis_analysis_service=SynthesisAnalysisService(
            sessionmaker,
            _E2eSynthesisModel(observer=observer, provider=provider, model_id=model_id),
        ),
    )


def _stage5_deps(
    sessionmaker, *, observer, provider, model_id, audit_factories
) -> Stage5WorkflowDependencies:
    draft_service = DraftSectionService(
        sessionmaker, _E2eDraftModel(observer=observer, provider=provider, model_id=model_id)
    )
    report_service = ReportService(sessionmaker, draft_service)
    check_service = ReportCheckService(sessionmaker, report_service)
    audit_service = ReportAuditService(
        sessionmaker,
        _E2eAuditModel(
            _SequencedFactories(audit_factories),
            observer=observer,
            provider=provider,
            model_id=model_id,
        ),
        check_service,
    )
    review_service = ReviewActionService(sessionmaker, audit_service)
    revision_service = RevisionService(
        sessionmaker,
        model=_E2eRevisionModel(observer=observer, provider=provider, model_id=model_id),
        draft_section_service=draft_service,
        check_service=check_service,
        review_action_service=review_service,
    )
    report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环
    return Stage5WorkflowDependencies(
        sessionmaker=sessionmaker,
        report_outline_service=ReportOutlineService(sessionmaker),
        draft_section_service=draft_service,
        report_service=report_service,
        report_check_service=check_service,
        report_audit_service=audit_service,
        review_action_service=review_service,
        revision_service=revision_service,
        research_backflow_service=ResearchBackflowService(
            sessionmaker, review_service, report_service
        ),
    )


def create_multi_stage_fake_bundle(
    config: EvalExecutionConfig, plan_payload: ResearchPlanPayload
) -> MultiStageModelFactoryBundle:
    """fake `MultiStageModelFactoryBundle`：身份 = frozen config.model，5 个 factory
    每次 run 构造 per-attempt fake 模型（绑定 usage_observer）。"""
    provider = config.model.provider
    model_id = config.model.model_id

    def _make_planner(obs):
        return _PlannerModel(plan_payload, observer=obs, provider=provider, model_id=model_id)

    def _make_evidence(obs):
        return _E2eEvidenceModel(observer=obs, provider=provider, model_id=model_id)

    def _make_claim(obs):
        return _E2eClaimModel(observer=obs, provider=provider, model_id=model_id)

    def _make_synthesis(obs):
        return _E2eSynthesisModel(observer=obs, provider=provider, model_id=model_id)

    def _make_draft(obs):
        return _E2eDraftModel(observer=obs, provider=provider, model_id=model_id)

    def _make_stage4_deps(sessionmaker, obs):
        return _stage4_deps(sessionmaker, observer=obs, provider=provider, model_id=model_id)

    return MultiStageModelFactoryBundle(
        provider=provider,
        model_id=model_id,
        create_planner=_make_planner,
        create_evidence=_make_evidence,
        create_claim=_make_claim,
        create_synthesis=_make_synthesis,
        create_draft=_make_draft,
        create_stage4_deps=_make_stage4_deps,
    )


def create_full_fake_bundle(
    config: EvalExecutionConfig,
    plan_payload: ResearchPlanPayload,
    audit_factories=(),
) -> FullModelFactoryBundle:
    """fake `FullModelFactoryBundle`：身份 = frozen config.model；10 个 factory
    每次 run 构造 per-attempt fake 模型（绑定 usage_observer）。

    `audit_factories`：callable 列表，audit fake 按调用次序取（最后重复）；默认
    pass（happy path）。
    """
    provider = config.model.provider
    model_id = config.model.model_id
    audit_factories = tuple(audit_factories) or (_pass_decision_factory(),)

    def _make_planner(obs):
        return _PlannerModel(plan_payload, observer=obs, provider=provider, model_id=model_id)

    def _make_evidence(obs):
        return _E2eEvidenceModel(observer=obs, provider=provider, model_id=model_id)

    def _make_claim(obs):
        return _E2eClaimModel(observer=obs, provider=provider, model_id=model_id)

    def _make_financial(obs):
        return _RecordingFixedModel(
            _financial_decision(),
            COMPONENT_FINANCIAL_ANALYSIS,
            observer=obs,
            provider=provider,
            model_id=model_id,
        )

    def _make_macro(obs):
        return _RecordingFixedModel(
            _macro_decision(),
            COMPONENT_MACRO_ANALYSIS,
            observer=obs,
            provider=provider,
            model_id=model_id,
        )

    def _make_valuation(obs):
        return _RecordingFixedModel(
            _valuation_decision(),
            COMPONENT_VALUATION_ANALYSIS,
            observer=obs,
            provider=provider,
            model_id=model_id,
        )

    def _make_synthesis(obs):
        return _E2eSynthesisModel(observer=obs, provider=provider, model_id=model_id)

    def _make_draft(obs):
        return _E2eDraftModel(observer=obs, provider=provider, model_id=model_id)

    def _make_audit(obs):
        return _E2eAuditModel(
            _SequencedFactories(audit_factories),
            observer=obs,
            provider=provider,
            model_id=model_id,
        )

    def _make_revision(obs):
        return _E2eRevisionModel(observer=obs, provider=provider, model_id=model_id)

    def _make_stage4_deps(sessionmaker, obs):
        return _stage4_deps(sessionmaker, observer=obs, provider=provider, model_id=model_id)

    def _make_stage5_deps(sessionmaker, obs):
        return _stage5_deps(
            sessionmaker,
            observer=obs,
            provider=provider,
            model_id=model_id,
            audit_factories=audit_factories,
        )

    return FullModelFactoryBundle(
        provider=provider,
        model_id=model_id,
        create_planner=_make_planner,
        create_evidence=_make_evidence,
        create_claim=_make_claim,
        create_financial=_make_financial,
        create_macro=_make_macro,
        create_valuation=_make_valuation,
        create_synthesis=_make_synthesis,
        create_draft=_make_draft,
        create_audit=_make_audit,
        create_revision=_make_revision,
        create_stage4_deps=_make_stage4_deps,
        create_stage5_deps=_make_stage5_deps,
    )
