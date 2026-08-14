"""LLM instrumentation component inventory (stage 7B.1.2B).

用「registry + 静态扫描」两重校验，防止以后有人新增 production DeepSeek adapter
却忘记走 instrumentation：

- `INSTRUMENTED_LLM_COMPONENTS` registry 必须 == Part G 审计冻结的 10 个
  component 集合；
- 静态扫描 `app/`（排除 `app/cli/` 与 `app/eval/benchmark/`——开发期 CLI /
  benchmark 工具）：凡用 `ChatDeepSeek(` 的文件必须恰好是那 10 个 production
  adapter，且每个都调用 `invoke_structured_with_usage(`。

`app/eval/benchmark/experiment.py` 的 real 模式在装配期构造 single_rag 的
`ChatDeepSeek`（注入 `DeepSeekSingleRagAnswerModel`，实际调用仍走
`invoke_structured_with_usage` wrapper）——与 `app/cli/` 同类：dev 工具，不是
production pipeline 调用点。

这不是脆弱的字符串 grep：断言的是「production LLM 调用点都经 wrapper」这一
结构化不变量，而不是 provider 名 / 环境变量等易变文本。
"""

from pathlib import Path

import app as app_pkg
from app.llm.components import INSTRUMENTED_LLM_COMPONENTS

_APP_DIR = Path(app_pkg.__file__).resolve().parent

# Part G 审计冻结的 10 个 production DeepSeek adapter 文件（repo-relative）。
_KNOWN_PRODUCTION_LLM_FILES = frozenset(
    {
        "app/evidence/extractor/adapters.py",
        "app/analysis/claims/adapters.py",
        "app/analysis/financial/adapters.py",
        "app/analysis/macro/adapters.py",
        "app/analysis/synthesis/adapters.py",
        "app/analysis/valuation/adapters.py",
        "app/draft_section/adapters.py",
        "app/audit/adapters.py",
        "app/revision/adapters.py",
        "app/research_planning/planner.py",
    }
)


def _production_chatdeepseek_files() -> set[str]:
    found: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        rel = path.relative_to(_APP_DIR.parent).as_posix()
        if rel.startswith(("app/cli/", "app/eval/benchmark/")):
            continue
        if "ChatDeepSeek(" in path.read_text(encoding="utf-8"):
            found.add(rel)
    return found


def test_instrumented_components_match_audited_set() -> None:
    assert INSTRUMENTED_LLM_COMPONENTS == (
        "evidence_extraction",
        "claim_analysis",
        "financial_analysis",
        "macro_analysis",
        "synthesis_analysis",
        "valuation_analysis",
        "draft_section_writer",
        "audit",
        "revision_writer",
        "research_planner",
    )


def test_all_production_llm_call_sites_are_instrumented() -> None:
    files = _production_chatdeepseek_files()
    assert files == _KNOWN_PRODUCTION_LLM_FILES
    for rel in sorted(files):
        text = (_APP_DIR.parent / rel).read_text(encoding="utf-8")
        assert "invoke_structured_with_usage(" in text, f"{rel} 未走 instrumentation wrapper"
