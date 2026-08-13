"""Evaluation runtime errors (stage 7B.1.0).

稳定错误层次：`EvalError` → `EvalContractError` / `EvalFingerprintError` /
`EvalVariantError` / `EvalMaterializationError`。错误消息**不**塞 payload /
labels / raw source text / 完整 fingerprint / API key。
"""


class EvalError(Exception):
    """Evaluation 稳定错误基类。"""

    code = "eval_error"


class EvalContractError(EvalError):
    """Eval 契约构造 / 校验失败（Pydantic 校验失败翻译为稳定 code）。"""

    code = "eval_contract_error"


class EvalFingerprintError(EvalError):
    """Eval fingerprint 计算 / 校验失败。"""

    code = "eval_fingerprint_error"


class EvalVariantError(EvalError):
    """Eval variant 非法（未知 variant id 等）。"""

    code = "eval_variant_error"


class EvalMaterializationError(EvalError):
    """Snapshot materialization 失败（缺失 / 跨公司 / 未来证据 / 字节篡改 /
    领域 verifier 校验失败等）。"""

    code = "eval_materialization_error"
