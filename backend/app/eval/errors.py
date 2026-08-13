"""Evaluation runtime errors (stage 7B.1.0).

稳定错误层次：`EvalError` → `EvalContractError` / `EvalFingerprintError` /
`EvalVariantError`。错误消息**不**塞 payload / labels / raw source text / 完整
fingerprint / API key。
"""


class EvalError(Exception):
    """Evaluation 稳定错误基类。"""


class EvalContractError(EvalError):
    """Eval 契约构造 / 校验失败（Pydantic 校验失败翻译为稳定 code）。"""


class EvalFingerprintError(EvalError):
    """Eval fingerprint 计算 / 校验失败。"""


class EvalVariantError(EvalError):
    """Eval variant 非法（未知 variant id 等）。"""
