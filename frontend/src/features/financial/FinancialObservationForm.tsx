/** 手动录入财务数据（V1.1 产品语义）。

用户从公司官方年报/半年报/季报转录财务数字；后端校验数字与引文一致
（quote_text 必须包含 source_value_text），成功创建证据卡。
 */

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  Select,
  Space,
} from 'antd';
import type { Dayjs } from 'dayjs';

import { createUserSuppliedFinancialObservation } from '../../api/financial';
import { ApiError } from '../../types/api';
import {
  BALANCE_SHEET_METRICS,
  FINANCIAL_METRIC_CODE,
  FINANCIAL_METRIC_LABELS,
  FINANCIAL_RAW_UNIT,
  FINANCIAL_RAW_UNIT_LABELS,
  type FinancialMetricCode,
  type FinancialObservationRequest,
  type FinancialRawUnit,
  type FinancialStatementScope,
} from '../../types/financial';
import type { SourceDocumentType } from '../../types/source';

interface FormValues {
  metric_code: FinancialMetricCode;
  period_start?: Dayjs;
  period_end: Dayjs;
  raw_unit: FinancialRawUnit;
  source_value_text: string;
  quote_text: string;
  source_title: string;
  source_url?: string;
}

interface Props {
  taskId: string;
  companyId: string | null;
}

export function FinancialObservationForm({ taskId, companyId }: Props): React.JSX.Element {
  const [form] = Form.useForm<FormValues>();
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const metricCode = Form.useWatch('metric_code', form);
  const isBalanceSheet =
    metricCode != null && BALANCE_SHEET_METRICS.includes(metricCode as FinancialMetricCode);

  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      // Part 5 简化：内部字段（口径/文档类型/证据陈述）由前端确定性派生，
      // 用户只填业务字段；provenance 校验（quote 含数字）由后端保留。
      const payload: FinancialObservationRequest = {
        metric_code: values.metric_code,
        statement_scope: 'consolidated' as FinancialStatementScope,
        period_start:
          isBalanceSheet || !values.period_start
            ? null
            : values.period_start.format('YYYY-MM-DD'),
        period_end: values.period_end.format('YYYY-MM-DD'),
        raw_unit: values.raw_unit,
        source_value_text: values.source_value_text.trim(),
        quote_text: values.quote_text.trim(),
        evidence_statement: `${FINANCIAL_METRIC_LABELS[values.metric_code]}（${values.period_end.format('YYYY-MM-DD')}）的数值为 ${values.source_value_text.trim()}`,
        source_title: values.source_title.trim(),
        source_url: values.source_url?.trim() || null,
        document_type: 'annual_report' as SourceDocumentType,
      };
      return createUserSuppliedFinancialObservation(taskId, payload);
    },
    onSuccess: () => {
      setSuccessMessage('财务数据已登记（证据卡已创建）');
      form.resetFields();
    },
    onError: () => {
      setSuccessMessage(null);
    },
  });

  const errorMessage = mutation.error instanceof ApiError ? mutation.error.message : null;

  if (!companyId) {
    return (
      <Card title="补充财务数据（可选）">
        <Alert type="warning" showIcon message="公司尚未解析，暂无法补充财务数据" />
      </Card>
    );
  }

  return (
    <Card title="补充财务数据（可选）">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="info"
          showIcon
          message="自动研究无法获取时，可在此补充公司官方年报中的财务数据；引文需包含该数字的原文，系统会校验数字与引文一致。"
        />
        <Form<FormValues>
          form={form}
          layout="vertical"
          onFinish={(values) => mutation.mutate(values)}
          disabled={mutation.isPending}
          initialValues={{
            raw_unit: 'hundred_million_yuan',
          }}
        >
          <Form.Item
            name="metric_code"
            label="指标"
            rules={[{ required: true, message: '请选择指标' }]}
          >
            <Select
              placeholder="选择财务指标"
              options={FINANCIAL_METRIC_CODE.map((code) => ({
                value: code,
                label: FINANCIAL_METRIC_LABELS[code],
              }))}
            />
          </Form.Item>

          {isBalanceSheet ? (
            <Alert
              type="info"
              showIcon
              message="资产负债表指标为期末时点，仅需期间结束日"
              style={{ marginBottom: 16 }}
            />
          ) : null}

          <Space size="middle" align="start" wrap>
            {!isBalanceSheet ? (
              <Form.Item name="period_start" label="期间开始日">
                <DatePicker placeholder="选择日期" />
              </Form.Item>
            ) : null}
            <Form.Item
              name="period_end"
              label={isBalanceSheet ? '期末日期' : '期间结束日'}
              rules={[{ required: true, message: '请选择日期' }]}
            >
              <DatePicker placeholder="选择日期" />
            </Form.Item>
          </Space>

          <Form.Item
            name="raw_unit"
            label="单位"
            rules={[{ required: true, message: '请选择单位' }]}
          >
            <Select
              options={FINANCIAL_RAW_UNIT.map((unit) => ({
                value: unit,
                label: FINANCIAL_RAW_UNIT_LABELS[unit],
              }))}
            />
          </Form.Item>

          <Form.Item
            name="source_value_text"
            label="数值"
            rules={[{ required: true, message: '请输入数值' }]}
          >
            <Input maxLength={100} placeholder="例如：4009.17" />
          </Form.Item>

          <Form.Item
            name="quote_text"
            label="原文引文（含该数字的句子）"
            rules={[{ required: true, message: '请输入原文引文' }]}
          >
            <Input.TextArea
              rows={3}
              maxLength={2000}
              placeholder="粘贴包含该数字的原文句子，例如：报告期内，公司实现营业收入4009.17亿元。"
            />
          </Form.Item>

          <Form.Item
            name="source_title"
            label="来源说明"
            rules={[{ required: true, message: '请输入来源说明' }]}
          >
            <Input maxLength={500} placeholder="例如：宁德时代2023年年度报告" />
          </Form.Item>

          <Form.Item name="source_url" label="原始链接（可选）">
            <Input maxLength={2000} placeholder="https://static.szse.cn/…" />
          </Form.Item>

          {errorMessage ? (
            <Alert type="error" showIcon message="提交失败" description={errorMessage} style={{ marginBottom: 16 }} />
          ) : null}
          {successMessage ? (
            <Alert type="success" showIcon message={successMessage} style={{ marginBottom: 16 }} />
          ) : null}

          <Button type="primary" htmlType="submit" loading={mutation.isPending}>
            提交财务数据
          </Button>
        </Form>
      </Space>
    </Card>
  );
}
