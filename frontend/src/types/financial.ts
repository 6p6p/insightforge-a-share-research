/** 手动录入财务数据（用户从官方报告转录）契约。
与后端 POST /tasks/{task_id}/financial-observations 对齐。
 */

import type { SourceDocumentType } from './source';

export const FINANCIAL_METRIC_CODE = [
  'revenue',
  'operating_cost',
  'operating_profit',
  'profit_before_tax',
  'net_profit',
  'net_profit_parent',
  'net_profit_parent_excl_nonrecurring',
  'operating_cash_flow_net',
  'total_assets',
  'total_liabilities',
  'equity_parent',
] as const;
export type FinancialMetricCode = (typeof FINANCIAL_METRIC_CODE)[number];

/** 指标 → 产品中文标签。 */
export const FINANCIAL_METRIC_LABELS: Record<FinancialMetricCode, string> = {
  revenue: '营业收入',
  operating_cost: '营业成本',
  operating_profit: '营业利润',
  profit_before_tax: '利润总额',
  net_profit: '净利润',
  net_profit_parent: '归母净利润',
  net_profit_parent_excl_nonrecurring: '扣非归母净利润',
  operating_cash_flow_net: '经营活动现金流量净额',
  total_assets: '总资产',
  total_liabilities: '总负债',
  equity_parent: '归母净资产',
};

export const FINANCIAL_RAW_UNIT = [
  'yuan',
  'thousand_yuan',
  'ten_thousand_yuan',
  'hundred_million_yuan',
] as const;
export type FinancialRawUnit = (typeof FINANCIAL_RAW_UNIT)[number];

/** 数值单位 → 产品中文标签。 */
export const FINANCIAL_RAW_UNIT_LABELS: Record<FinancialRawUnit, string> = {
  yuan: '元',
  thousand_yuan: '千元',
  ten_thousand_yuan: '万元',
  hundred_million_yuan: '亿元',
};

export const FINANCIAL_STATEMENT_SCOPE = ['consolidated', 'parent'] as const;
export type FinancialStatementScope = (typeof FINANCIAL_STATEMENT_SCOPE)[number];

/** 报表口径 → 产品中文标签。 */
export const FINANCIAL_STATEMENT_SCOPE_LABELS: Record<FinancialStatementScope, string> = {
  consolidated: '合并报表',
  parent: '母公司报表',
};

/** 资产负债表指标：期末时点值，period_start 必须为 null。 */
export const BALANCE_SHEET_METRICS: readonly FinancialMetricCode[] = [
  'total_assets',
  'total_liabilities',
  'equity_parent',
];

export interface FinancialObservationRequest {
  metric_code: FinancialMetricCode;
  statement_scope: FinancialStatementScope;
  /** ISO 日期（YYYY-MM-DD）；资产负债表指标为 null。 */
  period_start: string | null;
  /** ISO 日期（YYYY-MM-DD）。 */
  period_end: string;
  raw_unit: FinancialRawUnit;
  source_value_text: string;
  /** 必须包含 source_value_text 的原文句子（后端校验一致性）。 */
  quote_text: string;
  evidence_statement: string;
  source_title: string;
  source_url?: string | null;
  document_type: SourceDocumentType;
}

export interface FinancialObservationResponse {
  evidence_card_id: string;
  source_id: string;
  metric_observation_id: string;
  metric_fingerprint: string;
  replayed: boolean;
}
