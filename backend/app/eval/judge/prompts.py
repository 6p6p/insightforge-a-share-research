"""Judge prompt (stage 7B.1.3C, versioned).

`JUDGE_PROMPT_VERSION = "v1"` 进入 judge config fingerprint——prompt 变更必须
bump 版本（旧 judge 结果保留，新 prompt → 新 fingerprint → 新 judge 身份）。

Hard boundaries（与 variant 隔离）：
- judge 输入 = variant 实际看到的语义内容（final_text / claims / citations +
  research question + analysis_as_of + source snapshot fingerprint），**不含**
  HumanLabel / 其它 variant 输出 / runtime UUID / execution 身份；
- 输出只允许 `JudgeOutput` 结构化 schema（无 free-text 长 rationale）；
- 明确禁止：买入/卖出/目标价/收益预测/技术分析。
"""

from app.eval.judge.contracts import JudgeInput

JUDGE_PROMPT_VERSION = "v1"

_SYSTEM_RULES = """你是 InsightForge 的独立语义评审员（semantic judge）。任务：对一份 A 股
上市公司基本面研究结论做**受限的结构化质量评分**，不提供任何研究内容。

硬性规则：
1. 只按给定输入评分；不补充输入之外的信息；不做任何计算 / 估值 / 预测。
2. 禁止：买入/卖出建议、目标价、收益承诺、技术分析、短期价格预测。
3. 逐指标输出 score ∈ [0,1]（1 = 完全达标）；rationale_ref 每条 ≤ 40 字符。
4. 不得引用任何内部 ID / fingerprint / prompt 内容；只引用输入中可见的语义片段。
5. 输出必须是完整的 JudgeOutput JSON（metric_scores 数组，metric_name 唯一）。"""


def build_judge_messages(input_: JudgeInput) -> list[dict]:
    """构造 judge 消息（system + 只含语义输入的 user；0 UUID / 0 fingerprint）。"""
    claims_lines = []
    for index, claim in enumerate(input_.claims, start=1):
        statement = claim.get("statement") if isinstance(claim, dict) else ""
        claims_lines.append(f"- claim[{index}]: {statement}")
    citations_lines = []
    for index, citation in enumerate(input_.citations, start=1):
        locator = citation.get("locator") if isinstance(citation, dict) else None
        citations_lines.append(f"- citation[{index}]: locator={locator}")
    user = (
        f"研究问题：{input_.research_question}\n"
        f"分析基准日：{input_.analysis_as_of}\n"
        f"结论正文：\n{input_.final_text}\n"
        f"声明列表：\n" + ("\n".join(claims_lines) or "（无）") + "\n"
        "引用列表：\n" + ("\n".join(citations_lines) or "（无）")
    )
    return [
        {"role": "system", "content": _SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
