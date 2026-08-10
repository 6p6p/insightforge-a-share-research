"""Stage 4 LangGraph analysis workflow (spec 4D.2).

Graph topology:
    START → validate_analysis_plan → dispatch_parallel_analysis → (Send × N)
        → run_analysis_item → collect_claim_ids → synthesize_claims → END

- LangGraph 是唯一顶层编排器；nodes 只协调 Application Services，不重写业务逻辑。
- `Stage4WorkflowState` 只允许 checkpoint-safe 小对象（str / list[dict] /
  list[str]），UUID 统一 string；不放过 SQLAlchemy / Pydantic models /
  AsyncSession / LLM objects / Evidence text / prompt / reasoning_content。
- worker 完成顺序不影响最终 claim_ids（canonical sort + dedupe）。
- `synthesize_claims` 调用 SynthesisService + SynthesisAnalysisService（幂等，
  fingerprint / replay 无重复业务对象）。
"""
