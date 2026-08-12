"""Research planning (stage 7A.1): Research Planner + Deterministic Source Routing.

- **Planner**（`planner.py` / `service.py`）：ResearchTask → semantic ResearchPlan
  （bounded vocabulary，无内部 ID），input/plan fingerprint + replay + integrity
  verify（0 次额外 LLM）；
- **Router**（`router.py`）：0 LLM 的 deterministic SourceRoutePlan；
- **Preparation**（`preparation.py`）：按 ResearchPlan + SourceRoutePlan 从现有
  artifact repositories 解析资料 → auto Stage4 WorkPlan 或 MissingResearchNeeds。
"""
