"""Financial auto extraction errors (P3 Foundation)。"""


class FinancialExtractionError(RuntimeError):
    """稳定错误码（provenance 校验失败；不泄漏 block 正文）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
