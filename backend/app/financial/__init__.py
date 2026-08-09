"""Financial metric observation foundation (stage 4B.2A).

把来源于真实财务 Evidence 的**原始财务数值**登记为确定性的
`FinancialMetricObservation`，供后续（4B.2B）确定性财务计算。
本阶段不计算同比 / 环比 / margin / ratio、不调用 LLM、不自动从 PDF 表格
猜财务数字。
"""
