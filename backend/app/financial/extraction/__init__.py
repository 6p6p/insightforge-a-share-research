"""Financial auto extraction foundation (P3)。

接口层：FinancialExtractionProvider → numeric provenance 校验 →
FinancialExtractionService（不落库；observation 持久化后续 milestone）。
"""

from app.financial.extraction.contracts import (
    ExtractedFinancialObservation,
    FinancialExtractionProvider,
    FinancialExtractionRequest,
)
from app.financial.extraction.errors import FinancialExtractionError
from app.financial.extraction.service import FinancialExtractionResult, FinancialExtractionService

__all__ = [
    "ExtractedFinancialObservation",
    "FinancialExtractionError",
    "FinancialExtractionProvider",
    "FinancialExtractionRequest",
    "FinancialExtractionResult",
    "FinancialExtractionService",
]
