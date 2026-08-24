"""冻结校验：production LLM 调用点都经统一 factory + instrumentation wrapper。

v1.2.8 起，生产 adapter **禁止**直接实例化 ChatDeepSeek / ChatOpenAI，必须经
`app.llm.base.get_active_llm(settings)` 获取 LangChain-compatible ChatModel，
再经 `invoke_structured_with_usage(` 上报 usage。

用「registry + 静态扫描」两重校验，防止以后有人新增 production LLM adapter
却忘记走 instrumentation：

- `INSTRUMENTED_LLM_COMPONENTS` registry 必须 == 审计冻结的 12 个
  component 集合；
- 静态扫描 `app/`（排除 `app/cli/` 与 `app/eval/benchmark/`——开发期 CLI /
  benchmark 工具）：凡用 `get_active_llm(` 的文件必须恰好是那 12 个 production
  adapter，且每个都调用 `invoke_structured_with_usage(`；
- 直接实例化 `ChatDeepSeek(` / `ChatOpenAI(` 只允许出现在 `app/llm/base.py`
  （统一 factory），其他 app 文件出现即视为违规。

`app/eval/benchmark/experiment.py` 的 real 模式在装配期构造 single_rag 的
`ChatDeepSeek`——与 `app/cli/` 同类：dev 工具，不是 production pipeline
调用点（排除在扫描外）。

这不是脆弱的字符串 grep：断言的是「production LLM 调用点都经统一入口 +
wrapper」这一结构化不变量，而不是 provider 名 / 环境变量等易变文本。
"""

from pathlib import Path

import app as app_pkg
from app.llm.components import INSTRUMENTED_LLM_COMPONENTS

_APP_DIR = Path(app_pkg.__file__).resolve().parent

# 审计冻结的 12 个 production LLM adapter 文件（repo-relative）。
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
        "app/research_planning/intent.py",
        "app/services/source_discovery/search_model.py",
    }
)

# v1.2.8：唯一允许直接实例化 ChatDeepSeek / ChatOpenAI 的 factory 文件。
_ALLOWED_DIRECT_LLM_FACTORY = {"app/llm/base.py"}


def _production_llm_factory_files() -> set[str]:
    """扫描 app/（排除 cli 与 eval/benchmark）：凡调用 get_active_llm( 的文件。"""
    found: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        rel = path.relative_to(_APP_DIR.parent).as_posix()
        if rel.startswith(("app/cli/", "app/eval/benchmark/")):
            continue
        # factory 自身的定义文件与 docstring 提及不算调用点
        if rel in ("app/llm/base.py", "app/llm/active_config.py"):
            continue
        if "get_active_llm(" in path.read_text(encoding="utf-8"):
            found.add(rel)
    return found


def _direct_instantiation_files() -> set[str]:
    """app/（排除 cli 与 eval/benchmark）中直接实例化 ChatDeepSeek(/ChatOpenAI( 的文件。"""
    found: set[str] = set()
    for path in _APP_DIR.rglob("*.py"):
        rel = path.relative_to(_APP_DIR.parent).as_posix()
        if rel.startswith(("app/cli/", "app/eval/benchmark/")):
            continue
        text = path.read_text(encoding="utf-8")
        if "ChatDeepSeek(" in text or "ChatOpenAI(" in text:
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
        "intent_enhancement",
        "search_discovery",
    )


def test_all_production_llm_call_sites_use_factory() -> None:
    files = _production_llm_factory_files()
    assert files == _KNOWN_PRODUCTION_LLM_FILES
    for rel in sorted(files):
        text = (_APP_DIR.parent / rel).read_text(encoding="utf-8")
        assert "invoke_structured_with_usage(" in text, f"{rel} 未走 instrumentation wrapper"


def test_only_factory_instantiates_llm_directly() -> None:
    """直接实例化 ChatDeepSeek / ChatOpenAI 只允许在 app/llm/base.py（统一 factory）。"""
    files = _direct_instantiation_files()
    assert files == _ALLOWED_DIRECT_LLM_FACTORY, (
        f"直接实例化 LLM 的文件必须是 {_ALLOWED_DIRECT_LLM_FACTORY}，得到 {files}"
    )
