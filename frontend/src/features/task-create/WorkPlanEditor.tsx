/** 显式 Stage 4 work plan 编辑器（spec J/C）。

Stage 6A **不假装自动 source planning 已完成**：这里逐条暴露后端 execute
契约的字段（analysis_type + 各类真实 ID），由用户填写。每条 work item 对应
后端 stage4.contracts.AnalysisWorkItem 判别联合的一个分支。
 */

import { Button, Card, Input, Select, Space, Typography } from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';

import type { AnalysisType, AnalysisWorkItem } from '../../types/workspace';

const { Text } = Typography;

const ANALYSIS_TYPE_OPTIONS: { value: AnalysisType; label: string }[] = [
  { value: 'business', label: '业务分析' },
  { value: 'event', label: '事件分析' },
  { value: 'risk', label: '风险分析' },
  { value: 'financial', label: '财务分析' },
  { value: 'macro', label: '宏观分析' },
  { value: 'valuation', label: '估值分析' },
];

/** 按分析类型返回需要填写的 ID 字段。 */
function idFieldsFor(type: AnalysisType): { key: string; label: string; required: boolean }[] {
  switch (type) {
    case 'financial':
      return [
        { key: 'calculation_ids', label: '财务计算 ID（calculation_ids）', required: true },
        { key: 'additional_evidence_ids', label: '附加证据 ID（additional_evidence_ids，可选）', required: false },
      ];
    case 'macro':
      return [
        { key: 'macro_driver_evidence_ids', label: '宏观驱动证据 ID', required: true },
        { key: 'company_evidence_ids', label: '公司证据 ID', required: true },
      ];
    case 'valuation':
      return [{ key: 'comparison_ids', label: '相对估值对比 ID（comparison_ids）', required: true }];
    case 'business':
    case 'event':
    case 'risk':
      return [{ key: 'evidence_card_ids', label: '证据卡 ID（evidence_card_ids）', required: true }];
  }
}

function splitIds(text: string): string[] {
  return [...new Set(text.split(/[,\s\n]+/).map((s) => s.trim()).filter(Boolean))];
}

/** 生成只含当前类型 ID 字段的空工作项（丢弃旧类型遗留字段）。 */
function blankItemFor(type: AnalysisType, item_id: string): AnalysisWorkItem {
  const base = { item_id, analysis_type: type } as AnalysisWorkItem;
  for (const field of idFieldsFor(type)) {
    (base as unknown as Record<string, string[]>)[field.key] = [];
  }
  return base;
}

interface Props {
  value: AnalysisWorkItem[];
  onChange: (items: AnalysisWorkItem[]) => void;
  disabled?: boolean;
}

export function WorkPlanEditor({ value, onChange, disabled = false }: Props): React.JSX.Element {
  const update = (index: number, patch: Partial<AnalysisWorkItem>): void => {
    const next = value.map((item, i) => (i === index ? ({ ...item, ...patch } as AnalysisWorkItem) : item));
    onChange(next);
  };

  const setIds = (index: number, field: string, text: string): void => {
    const ids = splitIds(text);
    const item = value[index];
    const patch = { ...item, [field]: ids } as unknown as Partial<AnalysisWorkItem>;
    update(index, patch);
  };

  const addItem = (): void => {
    const nextId = `wi-${value.length + 1}`;
    onChange([...value, blankItemFor('business', nextId)]);
  };

  const removeItem = (index: number): void => {
    onChange(value.filter((_, i) => i !== index));
  };

  return (
    <div>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {value.length === 0 ? (
          <Text type="secondary">尚未添加执行工作项。Stage 6A 不包含自动 Source Planning，请手动输入工作项。每个工作项需要引用已入库的真实证据 / 计算 / 对比 ID。</Text>
        ) : null}
        {value.map((item, index) => {
          const fields = idFieldsFor(item.analysis_type);
          return (
            <Card
              key={index}
              size="small"
              title={`工作项 ${index + 1}`}
              extra={
                <Button
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={disabled}
                  onClick={() => removeItem(index)}
                  aria-label={`删除工作项 ${index + 1}`}
                >
                  删除
                </Button>
              }
            >
              <Space direction="vertical" style={{ width: '100%' }}>
                <Select
                  value={item.analysis_type}
                  options={ANALYSIS_TYPE_OPTIONS}
                  disabled={disabled}
                  style={{ width: 220 }}
                  aria-label={`工作项 ${index + 1} 分析类型`}
                  onChange={(type: AnalysisType) => {
                    // 整体替换为当前类型的空工作项，丢弃旧类型遗留字段。
                    onChange(value.map((v, i) => (i === index ? blankItemFor(type, v.item_id) : v)));
                  }}
                />
                {fields.map((field) => (
                  <div key={field.key}>
                    <div style={{ marginBottom: 4 }}>
                      <Text type="secondary">
                        {field.label}
                        {field.required ? '（必填）' : ''}
                      </Text>
                    </div>
                    <Input.TextArea
                      rows={2}
                      placeholder="多个 ID 用逗号或换行分隔"
                      disabled={disabled}
                      value={(item as unknown as Record<string, string[]>)[field.key]?.join(', ') ?? ''}
                      onChange={(e) => setIds(index, field.key, e.target.value)}
                      aria-label={`工作项 ${index + 1} ${field.key}`}
                    />
                  </div>
                ))}
              </Space>
            </Card>
          );
        })}
        <Button
          icon={<PlusOutlined />}
          disabled={disabled}
          onClick={addItem}
        >
          添加工作项
        </Button>
      </Space>
    </div>
  );
}
