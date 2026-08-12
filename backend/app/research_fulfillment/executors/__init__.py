"""Fulfillment executors (stage 7A.2A spec J/M/N/O/P): 自动补证据。

- `DocumentNeedExecutor`（document.py）：document / event need → Retrieval →
  Evidence（spec J/K/L）；
- `FinancialNeedExecutor`（financial.py）：financial need → calculation →
  re-preparation（spec M）；
- `MacroNeedExecutor`（macro.py）：macro need → macro Evidence replay（spec N）；
- `ValuationNeedExecutor`（valuation.py）：valuation need → manual_required
  + explicit_peer_set_required（spec O）。

executor **不抛**确定性错误：补证据失败 → attempt.status / error_code。
"""

from app.research_fulfillment.executors.document import (
    DocumentNeedExecutor,
    SourceIndexBuilder,
)
from app.research_fulfillment.executors.financial import FinancialNeedExecutor
from app.research_fulfillment.executors.macro import MacroNeedExecutor
from app.research_fulfillment.executors.valuation import ValuationNeedExecutor

__all__ = (
    "DocumentNeedExecutor",
    "SourceIndexBuilder",
    "FinancialNeedExecutor",
    "MacroNeedExecutor",
    "ValuationNeedExecutor",
)
