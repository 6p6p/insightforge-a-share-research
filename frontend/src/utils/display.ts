/** 产品展示映射（Part 3/4 Hardening）：后端枚举保持英文，前端显示中文。 */

/** source_type / document_type → 中文（未知原样返回）。 */
export const SOURCE_TYPE_LABEL: Record<string, string> = {
  annual_report: '年度报告',
  semiannual_report: '半年报',
  quarterly_report: '季度报告',
  news_article: '新闻事件',
  company_announcement: '公司公告',
  issuer_ir_material: '公司官网/IR',
  prospectus: '招股说明书',
  macro_series: '宏观数据',
  other: '其他资料',
};

/** provider_key → 中文（未知原样返回）。 */
export const PROVIDER_LABEL: Record<string, string> = {
  eastmoney: '东方财富',
  world_bank: '世界银行',
  issuer_official: '公司官网',
  gdelt: '全球新闻数据库',
  model_web_search: 'AI搜索发现',
  xinhuanet: '新华社',
  cnstock: '中国证券报',
  cs_com_cn: '中国证券网',
  csrc: '中国证监会',
  sse: '上交所',
  szse: '深交所',
  bse: '北交所',
  cninfo: '巨潮资讯',
  nbs: '国家统计局',
  fred: 'FRED',
  user_supplied: '用户提供',
};

/** evidence type → 中文（未知原样返回）。 */
export const EVIDENCE_TYPE_LABEL: Record<string, string> = {
  statement: '财务报表',
  metric: '指标数据',
  fact: '事实信息',
  event: '事件',
  context: '背景资料',
};

/** evidence origin_type → 中文（未知原样返回）。 */
export const EVIDENCE_ORIGIN_LABEL: Record<string, string> = {
  document_chunk: '文档内容',
  financial_extraction: '财务自动提取',
  macro_observation: '宏观观察',
  user_supplied: '用户提供',
};

export function displayLabel(mapping: Record<string, string>, value: string | null | undefined): string {
  if (!value) {
    return '—';
  }
  return mapping[value] ?? value;
}