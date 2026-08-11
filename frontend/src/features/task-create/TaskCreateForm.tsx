/** Task Create 表单（spec J）。

Stage6A 只做实际后端当前支持的字段：公司、研究起止日期、模块、研究问题、
相对估值开关。提交后 create task → （可选）显式 work plan execute → 跳转
/tasks/:taskId。
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
  Switch,
} from 'antd';
import type { Dayjs } from 'dayjs';

import { createTask, executeTask } from '../../api/tasks';
import { ApiError } from '../../types/api';
import { type ResearchModule, type TaskCreateRequest } from '../../types/task';
import type { AnalysisWorkItem } from '../../types/workspace';
import { WorkPlanEditor } from './WorkPlanEditor';

const MODULE_OPTIONS: { value: ResearchModule; label: string }[] = [
  { value: 'company_profile', label: '公司概况' },
  { value: 'business', label: '业务' },
  { value: 'financial', label: '财务' },
  { value: 'events', label: '事件' },
  { value: 'macro', label: '宏观' },
  { value: 'risk', label: '风险' },
];

interface FormValues {
  company_query: string;
  research_dates: [Dayjs, Dayjs];
  modules: ResearchModule[];
  questions: string;
  include_relative_valuation: boolean;
}

interface Props {
  onCreated: (taskId: string) => void;
}

export function TaskCreateForm({ onCreated }: Props): React.JSX.Element {
  const [form] = Form.useForm<FormValues>();
  const [workItems, setWorkItems] = useState<AnalysisWorkItem[]>([]);
  const [enableExecute, setEnableExecute] = useState(false);

  const mutation = useMutation({
    mutationFn: async (values: FormValues) => {
      const [start, end] = values.research_dates;
      const payload: TaskCreateRequest = {
        company_query: values.company_query.trim(),
        research_start_date: start.format('YYYY-MM-DD'),
        research_end_date: end.format('YYYY-MM-DD'),
        modules: values.modules,
        questions: (values.questions ?? '')
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean),
        include_relative_valuation: values.include_relative_valuation,
        require_plan_approval: true,
      };
      const task = await createTask(payload);
      if (enableExecute && workItems.length > 0) {
        await executeTask(task.task_id, { analysis_work_items: workItems });
      }
      return task.task_id;
    },
    onSuccess: (taskId) => onCreated(taskId),
  });

  const submit = (values: FormValues): void => {
    mutation.mutate(values);
  };

  const errorMessage =
    mutation.error instanceof ApiError ? mutation.error.message : mutation.error?.message;

  return (
    <Card title="新建研究任务">
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={submit}
        initialValues={{ include_relative_valuation: false }}
        requiredMark
      >
        <Form.Item
          name="company_query"
          label="公司名称 / 代码"
          rules={[{ required: true, message: '请输入公司名称或证券代码' }]}
        >
          <Input placeholder="例如：贵州茅台 或 600519" maxLength={100} />
        </Form.Item>

        <Form.Item
          name="research_dates"
          label="研究分析周期"
          rules={[{ required: true, message: '请选择研究起止日期' }]}
        >
          <DatePicker.RangePicker />
        </Form.Item>

        <Form.Item
          name="modules"
          label="研究模块"
          rules={[{ required: true, message: '请至少选择一个研究模块' }]}
        >
          <Select
            mode="multiple"
            options={MODULE_OPTIONS}
            placeholder="选择研究模块"
          />
        </Form.Item>

        <Form.Item
          name="questions"
          label="研究问题（每行一个）"
          tooltip="执行真实研究时以第一个问题为研究问题来源。"
        >
          <Input.TextArea rows={4} placeholder="例如：\n贵州茅台 2026 年营收与估值是否合理？" />
        </Form.Item>

        <Form.Item name="include_relative_valuation" label="包含相对估值" valuePropName="checked">
          <Switch />
        </Form.Item>

        <Form.Item label="执行研究（可选）">
          <Space direction="vertical" style={{ width: '100%' }}>
            <Switch
              checked={enableExecute}
              onChange={setEnableExecute}
              checkedChildren="创建后执行研究"
              unCheckedChildren="仅创建任务"
              aria-label="是否执行研究"
            />
            {enableExecute ? (
              <>
                <Alert
                  type="info"
                  showIcon
                  message="Stage 6A 不包含自动 Source Planning。请显式填写 Stage 4 work plan（引用已入库的真实证据 / 计算 / 对比 ID）。"
                />
                <WorkPlanEditor value={workItems} onChange={setWorkItems} />
              </>
            ) : null}
          </Space>
        </Form.Item>

        {errorMessage ? (
          <Alert type="error" showIcon message="创建失败" description={errorMessage} style={{ marginBottom: 16 }} />
        ) : null}

        <Button
          type="primary"
          htmlType="submit"
          loading={mutation.isPending}
          disabled={enableExecute && workItems.length === 0}
        >
          {enableExecute && workItems.length > 0 ? '创建并执行研究' : '创建任务'}
        </Button>
      </Form>
    </Card>
  );
}
