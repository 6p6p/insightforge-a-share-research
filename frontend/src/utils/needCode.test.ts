import { describe, expect, it } from 'vitest';

import { needCodeLabel } from './needCode';

describe('needCodeLabel（need code → 产品中文术语）', () => {
  it('文档类 need code → 报告/公告术语', () => {
    expect(needCodeLabel('annual_report_financial')).toBe('年度报告');
    expect(needCodeLabel('semiannual_report')).toBe('半年度报告');
    expect(needCodeLabel('quarterly_report_2023')).toBe('季度报告');
    expect(needCodeLabel('company_announcement')).toBe('公司公告');
    expect(needCodeLabel('issuer_ir_material')).toBe('公司官网资料');
    expect(needCodeLabel('prospectus')).toBe('招股书');
    expect(needCodeLabel('news_article')).toBe('新闻报道');
    expect(needCodeLabel('audit_report')).toBe('审计报告');
  });

  it('领域类 need code → 数据/信息术语', () => {
    expect(needCodeLabel('macro:gdp_growth')).toBe('宏观数据');
    expect(needCodeLabel('financial:revenue')).toBe('财务数据');
    expect(needCodeLabel('valuation:pe_ratio')).toBe('估值数据');
    expect(needCodeLabel('risk:litigation')).toBe('风险信息');
    expect(needCodeLabel('event:executive_change')).toBe('事件信息');
  });

  it('最具体关键字优先（annual_report_financial 不落为财务数据）', () => {
    expect(needCodeLabel('annual_report_financial')).toBe('年度报告');
  });

  it('未命中的 code 原样返回', () => {
    expect(needCodeLabel('revenue_growth')).toBe('revenue_growth');
    expect(needCodeLabel('')).toBe('');
  });
});
