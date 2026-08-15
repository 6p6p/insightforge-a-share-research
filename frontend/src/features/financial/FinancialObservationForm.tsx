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
  FINANCIAL_STATEMENT_SCOPE,
  FINANCIAL_STATEMENT_SCOPE_LABELS,
  type FinancialMetricCode,
  type FinancialObservationRequest,
  type FinancialRawUnit,
  type FinancialStatementScope,
} from '../../types/financial';
import {
  SOURCE_DOCUMENT_TYPE,
  SOURCE_DOCUMENT_TYPE_LABELS,
  type SourceDocumentType,
} from '../../types/source';

interface FormValues {
  metric_code: FinancialMetricCode;
  statement_scope: FinancialStatementScope;
  period_start?: Dayjs;
  period_end: Dayjs;
  raw_unit: FinancialRawUnit;
  source_value_text: string;
  quote_text: string;
  evidence_statement: string;
  source_title: string;
  source_url?: string;
  document_type: SourceDocumentType;
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
      const payload: FinancialObservationRequest = {
        metric_code: values.metric_code,
        statement_scope: values.statement_scope,
        period_start:
          isBalanceSheet || !values.period_start
            ? null
            : values.period_start.format('YYYY-MM-DD'),
        period_end: values.period_end.format('YYYY-MM-DD'),
        raw_unit: values.raw_unit,
        source_value_text: values.source_value_text.trim(),
        quote_text: values.quote_text.trim(),
        evidence_statement: values.evidence_statement.trim(),
        source_title: values.source_title.trim(),
        source_url: values.source_url?.trim() || null,
        document_type: values.document_type,
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
      <Card title="手动录入财务数据">
        <Alert type="warning" showIcon message="公司尚未解析，暂无法录入财务数据" />
      </Card>
    );
  }

  return (
    <Card title="手动录入财务数据">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="info"
          showIcon
          message="从公司官方年报/半年报/季报转录财务数字；引文必须包含该数字的原文，系统会校验数字与引文一致。"
        />
        <Form<FormValues>
          form={form}
          layout="vertical"
          onFinish={(values) => mutation.mutate(values)}
          disabled={mutation.isPending}
          initialValues={{
            statement_scope: 'consolidated',
            raw_unit: 'hundred_million_yuan',
            document_type: 'annual_report',
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

          <Form.Item
            name="statement_scope"
            label="报表口径"
            rules={[{ required: true, message: '请选择报表口径' }]}
          >
            <Select
              options={FINANCIAL_STATEMENT_SCOPE.map((scope) => ({
                value: scope,
                label: FINANCIAL_STATEMENT_SCOPE_LABELS[scope],
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
            label="数值单位"
            rules={[{ required: true, message: '请选择数值单位' }]}
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
            label="数值原文"
            rules={[{ required: true, message: '请输入数值原文' }]}
          >
            <Input maxLength={100} placeholder="例如：4009.17" />
          </Form.Item>

          <Form.Item
            name="quote_text"
            label="原文引文"
            rules={[{ required: true, message: '请输入原文引文' }]}
          >
            <Input.TextArea
              rows={3}
              maxLength={2000}
              placeholder="粘贴包含该数字的原文句子，例如：报告期内，公司实现营业收入4009.17亿元。"
            />
          </Form.Item>

          <Form.Item
            name="evidence_statement"
            label="证据陈述"
            rules={[{ required: true, message: '请输入证据陈述' }]}
          >
            <Input maxLength={500} placeholder="例如：2023 年度公司实现营业收入 4009.17 亿元" />
          </Form.Item>

          <Form.Item
            name="source_title"
            label="来源标题"
            rules={[{ required: true, message: '请输入来源标题' }]}
          >
            <Input maxLength={500} placeholder="例如：宁德时代2023年年度报告" />
          </Form.Item>

          <Form.Item name="source_url" label="原始链接（可选，官方披露链接）">
            <Input maxLength={2000} placeholder="https://static.szse.cn/…" />
          </Form.Item>

          <Form.Item
            name="document_type"
            label="文档类型"
            rules={[{ required: true, message: '请选择文档类型' }]}
          >
            <Select
              options={SOURCE_DOCUMENT_TYPE.map((type) => ({
                value: type,
                label: SOURCE_DOCUMENT_TYPE_LABELS[type],
              }))}
            />
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
