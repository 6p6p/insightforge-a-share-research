/** 手动研究方案编辑器（V1.1 产品语义）。

每条研究条目对应一个分析模块，引用已入库的真实证据 / 计算 / 对比 ID
（自动研究方案由系统规划；手动方案不包含自动资料收集）。

V1.1：界面文案产品化（不再暴露 Stage/plan/work item 开发术语），
字段标签只保留中文语义，ID 输入框用通用占位符。
 */

import { Button, Collapse, Input, Select, Space, Typography } from 'antd';
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

/** 按分析类型返回需要填写的 ID 字段（中文标签 + 内部字段名）。 */
function idFieldsFor(type: AnalysisType): { key: string; label: string; required: boolean }[] {
  switch (type) {
    case 'financial':
      return [
        { key: 'calculation_ids', label: '财务计算', required: true },
        { key: 'additional_evidence_ids', label: '附加证据（可选）', required: false },
      ];
    case 'macro':
      return [
        { key: 'macro_driver_evidence_ids', label: '宏观驱动证据', required: true },
        { key: 'company_evidence_ids', label: '公司证据', required: true },
      ];
    case 'valuation':
      return [{ key: 'comparison_ids', label: '相对估值对比', required: true }];
    case 'business':
    case 'event':
    case 'risk':
      return [{ key: 'evidence_card_ids', label: '证据', required: true }];
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
          <Text type="secondary">尚未添加研究条目。请手动添加研究条目，每条引用已入库的真实证据 / 计算 / 对比 ID。</Text>
        ) : null}
        {value.length > 0 ? (
          <Collapse
            items={value.map((item, index) => {
              const fields = idFieldsFor(item.analysis_type);
              return {
                key: String(index),
                label: `研究条目 ${index + 1}`,
                extra: (
                  <Button
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={disabled}
                    onClick={(e) => {
                      // extra 点击会冒泡触发展开/收起，先阻止再删除。
                      e.stopPropagation();
                      removeItem(index);
                    }}
                    aria-label={`删除研究条目 ${index + 1}`}
                  >
                    删除
                  </Button>
                ),
                children: (
                  <Space direction="vertical" style={{ width: '100%' }}>
                    <Select
                      value={item.analysis_type}
                      options={ANALYSIS_TYPE_OPTIONS}
                      disabled={disabled}
                      style={{ width: 220 }}
                      aria-label={`研究条目 ${index + 1} 分析类型`}
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
                          aria-label={`研究条目 ${index + 1} ${field.label}`}
                        />
                      </div>
                    ))}
                  </Space>
                ),
              };
            })}
            defaultActiveKey={value.map((_, i) => String(i))}
            collapsible={disabled ? 'disabled' : undefined}
          />
        ) : null}
        <Button
          icon={<PlusOutlined />}
          disabled={disabled}
          onClick={addItem}
        >
          添加研究条目
        </Button>
      </Space>
    </div>
  );
}
