"""LLM runtime contracts (stage 3C.2.1).

`create_evidence_extraction_model(settings)` 是 Evidence domain 获取 LLM
adapter 的唯一入口：domain/service 不直接 import provider SDK，也不读取环境
变量。工厂签名用 Protocol 冻结，便于测试替换。
"""

from typing import Protocol

from app.core.config import Settings
from app.evidence.extractor.contracts import EvidenceExtractionModel

# 当前受支持的 provider（第一版仅 DeepSeek）。
LLM_PROVIDER_DEEPSEEK = "deepseek"


class EvidenceExtractionModelFactory(Protocol):
    """把 Settings 映射为 EvidenceExtractionModel（不读环境变量，只读 Settings）。"""

    def __call__(self, settings: Settings) -> EvidenceExtractionModel: ...
