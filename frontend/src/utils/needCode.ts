/** need_code → 产品中文术语（V1.1）。

后端 research need code（如 'annual_report_financial'、'macro:gdp_growth'）对用户
不可读；这里按关键字（最长/最具体优先）映射为产品术语，未命中时原样返回。
 */

interface NeedCodeRule {
  keyword: string;
  label: string;
}

/** 有序规则：越靠前越具体，优先匹配。
 * 注意 'semiannual_report' 包含 'annual_report' 子串，必须排在前面。 */
const NEED_CODE_RULES: NeedCodeRule[] = [
  { keyword: 'semiannual_report', label: '半年度报告' },
  { keyword: 'annual_report', label: '年度报告' },
  { keyword: 'quarterly_report', label: '季度报告' },
  { keyword: 'company_announcement', label: '公司公告' },
  { keyword: 'issuer_ir_material', label: '公司官网资料' },
  { keyword: 'prospectus', label: '招股书' },
  { keyword: 'news_article', label: '新闻报道' },
  { keyword: 'audit_report', label: '审计报告' },
  { keyword: 'macro', label: '宏观数据' },
  { keyword: 'valuation', label: '估值数据' },
  { keyword: 'financial', label: '财务数据' },
  { keyword: 'risk', label: '风险信息' },
  { keyword: 'event', label: '事件信息' },
];

/** need code → 产品中文术语；未命中时原样返回。 */
export function needCodeLabel(code: string): string {
  if (!code) {
    return code;
  }
  for (const rule of NEED_CODE_RULES) {
    if (code.includes(rule.keyword)) {
      return rule.label;
    }
  }
  return code;
}
