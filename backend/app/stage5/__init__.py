"""Stage 5 report control workflow submodule (spec 5E.2A D/O/Q).

确定性编排控制环：Report → Check → Audit → ReviewAction → rewrite（bounded）／
human_review（interrupt）／finalize／research_required。LangGraph 是唯一顶层编排。
"""

from app.stage5.errors import Stage5WorkflowError

__all__ = ["Stage5WorkflowError"]
