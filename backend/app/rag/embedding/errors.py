"""Stable error taxonomy for embedding (stage 3B.1).

错误消息不包含：原文正文、完整 embedding 向量、DB URL、absolute path。
"""


class EmbeddingError(Exception):
    """embedding 稳定错误基类。"""

    code = "embedding_error"
    message = "embedding error"

    def __init__(self, message: str | None = None) -> None:
        # 未传 message 时使用类级默认（稳定默认 message），str() 即返回该值；
        # 传了 message 时保留既有按位置传参的调用语义不变。
        super().__init__(message if message is not None else self.message)


class EmbeddingContractError(EmbeddingError):
    """embedding 契约校验失败：dimension / finite / L2 norm 不满足模型契约。

    任何 embedding 必须满足：dimension=模型维度、分量全部有限、L2 norm≈1
    （normalize_embeddings=true 时）。这是防御与测试用内部不变量。
    """

    code = "embedding_contract_error"


class EmbeddingInputTooLong(EmbeddingError):
    """输入 token 数（含 special tokens）超过模型 max_input_tokens。

    禁止 silent truncation：不截断，直接抛错，由调用方决定如何降级。
    """

    code = "embedding_input_too_long"
    message = "embedding input too long"


class EmbeddingModelNotConfigured(EmbeddingError):
    """模型未配置完成（如 immutable revision 未确认），provider 拒绝加载。

    绝不回退到 moving "main" revision；自动化测试使用 FakeEmbeddingProvider，
    不下载真实模型。
    """

    code = "embedding_model_not_configured"
    message = "embedding model not configured"
