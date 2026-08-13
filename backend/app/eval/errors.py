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


class EvalSingleRagInputError(EvalVariantError):
    """single_rag variant 收到不支持的输入。

    v1 只支持 document-only（>=1 document source；macro_snapshots 与
    structured_artifacts 必须为空）。macro/structured 非空 → 稳定 fail-fast。
    """

    code = "single_rag_input_not_supported"


class EvalMaterializationError(EvalError):
    """Snapshot materialization 失败（缺失 / 跨公司 / 未来证据 / 字节篡改 /
    领域 verifier 校验失败等）。"""

    code = "eval_materialization_error"


class EvalScoringError(EvalError):
    """Deterministic scoring 失败（请求未实现的 metric calculator 等）。"""

    code = "eval_scoring_error"


class EvalOutputStructureError(EvalError):
    """normalized variant output 结构校验失败（duplicate id / dangling ref /
    source 未命中 frozen snapshot）。"""

    code = "eval_output_structure_error"


class EvalExecutionAssemblyError(EvalError):
    """execution benchmark 装配失败（spec↔case↔snapshot / spec↔trial↔attempt
    fingerprint 不一致）。

    这是 **benchmark assembly corruption**，不是 variant 执行失败：harness 必须在
    调用 runner **之前** fail-fast（runner 0 calls），**不得**收敛为 failed
    `EvalExecutionAttemptResult`。
    """

    code = "eval_execution_assembly_error"


class EvalPersistenceError(EvalError):
    """Evaluation execution persistence 失败（行不存在 / 无法持久化等）。

    独立于 `EvalMaterializationError`（那是 frozen bundle 物化失败）。错误消息
    **不**包含 prompt / output 文本 / token 明细 payload / API key / raw JSON。
    """

    code = "eval_persistence_error"


class EvalPersistenceIntegrityError(EvalPersistenceError):
    """Evaluation execution persistence 完整性破坏（fingerprint 不一致 / 篡改 /
    replay 不一致 / 父行身份不匹配）。"""

    code = "eval_persistence_integrity_error"


class EvalReplayError(EvalError):
    """Frozen bundle rehydration 失败（隔离运行时复现 frozen input 失败）。

    独立于 `EvalMaterializationError`（那是 PG→Bundle 物化失败）。错误消息
    **不**包含 raw bytes / source payload / DB URL / labels / prompt / API key。
    """

    code = "eval_replay_error"


class EvalReplayIntegrityError(EvalReplayError):
    """Rehydration 完整性破坏（SHA-256 mismatch / tamper / 语义字段不一致 /
    bundle 不自洽等）。"""

    code = "eval_replay_integrity_error"
