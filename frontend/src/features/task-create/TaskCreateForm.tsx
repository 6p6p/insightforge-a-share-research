/** Task Create 表单（V1.1 产品语义）。

V1.1 产品冻结：一次研究任务 = 一个核心研究问题；研究方案由系统自动规划，
不再提供手动指定研究方案（executeTask / work plan 入口已从正常用户流程移除）。

两阶段提交语义（P2-1 孤儿任务收口）：
1. 创建任务（POST /tasks）——成功即任务存在（带 task_id）；
2. 尝试自动开始研究（POST /tasks/{id}/orchestrations）——失败**不误报
   「创建失败」**，任务已创建；提供「重新启动研究」与「查看任务」。
 */

import { useRef, useState } from 'react';
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
  Typography,
} from 'antd';
import dayjs, { type Dayjs } from 'dayjs';

import { createOrchestration } from '../../api/orchestrations';
import { createTask } from '../../api/tasks';
import { ApiError } from '../../types/api';
import { type ResearchModule, type TaskCreateRequest } from '../../types/task';

const { Text } = Typography;

const MODULE_OPTIONS: { value: ResearchModule; label: string }[] = [
  { value: 'company_profile', label: '公司概况' },
  { value: 'business', label: '业务' },
  { value: 'financial', label: '财务' },
  { value: 'events', label: '事件' },
  { value: 'macro', label: '宏观' },
  { value: 'risk', label: '风险' },
];

/** AUTO 模式默认值：全部研究模块 + 默认分析窗口（近 3 年至今）。 */
const DEFAULT_MODULES = MODULE_OPTIONS.map((option) => option.value);
const DEFAULT_DATES: [Dayjs, Dayjs] = [dayjs().subtract(3, 'year'), dayjs()];

interface FormValues {
  company_query: string;
  research_dates: [Dayjs, Dayjs];
  modules: ResearchModule[];
  questions: string;
}

interface Props {
  onCreated: (taskId: string) => void;
}

export function TaskCreateForm({ onCreated }: Props): React.JSX.Element {
  const [form] = Form.useForm<FormValues>();
  const [autoStart, setAutoStart] = useState(true);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  // 防连发：同一时刻只允许一次提交（Enter 键 / 双击 / 重渲染均不重复创建任务）。
  const submittingRef = useRef(false);

  const createMutation = useMutation({
    mutationFn: async (values: FormValues) => {
      const [start, end] = values.research_dates;
      const payload: TaskCreateRequest = {
        company_query: values.company_query.trim(),
        research_start_date: start.format('YYYY-MM-DD'),
        research_end_date: end.format('YYYY-MM-DD'),
        modules: values.modules,
        questions: values.questions?.trim() ? [values.questions.trim()] : [],
        require_plan_approval: false,
      };
      return await createTask(payload);
    },
  });

  const startMutation = useMutation({
    mutationFn: (id: string) => createOrchestration(id),
    onSuccess: () => {
      if (taskId) {
        onCreated(taskId);
      }
    },
    onError: (error: unknown) => {
      const message =
        error instanceof ApiError ? error.message : '自动研究启动失败，请稍后重试';
      setStartError(message);
    },
  });

  /** 两阶段提交：先创建任务，再按 autoStart 尝试自动启动研究。 */
  const submit = async (values: FormValues): Promise<void> => {
    if (submittingRef.current) {
      return;
    }
    submittingRef.current = true;
    try {
      setStartError(null);
      const task = await createMutation.mutateAsync(values);
      setTaskId(task.task_id);
      if (autoStart) {
        startMutation.mutate(task.task_id);
      } else {
        onCreated(task.task_id);
      }
    } finally {
      submittingRef.current = false;
    }
  };

  const retryStart = (): void => {
    if (taskId) {
      setStartError(null);
      startMutation.mutate(taskId);
    }
  };

  const errorMessage =
    createMutation.error instanceof ApiError
      ? createMutation.error.message
      : createMutation.error?.message;

  const submitLabel = autoStart ? '创建并自动开始研究' : '创建任务';
  const starting = createMutation.isPending || startMutation.isPending;

  return (
    <Card title="新建研究任务">
      <Form<FormValues>
        form={form}
        layout="vertical"
        onFinish={(values) => void submit(values)}
        requiredMark
        initialValues={{ modules: DEFAULT_MODULES, research_dates: DEFAULT_DATES }}
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
          label="核心研究问题（可选）"
          tooltip="可选。不填时系统将根据公司、模块与日期自动生成默认研究意图"
          rules={[]}
        >
          <Input placeholder="可选：例如宁德时代近三年的盈利能力和增长驱动发生了什么变化？" maxLength={500} />
        </Form.Item>

        <Form.Item label="自动研究">
          <Switch
            checked={autoStart}
            onChange={setAutoStart}
            checkedChildren="创建后自动开始研究"
            unCheckedChildren="仅创建任务"
            aria-label="是否自动开始研究"
          />
        </Form.Item>

        {errorMessage ? (
          <Alert type="error" showIcon message="创建失败" description={errorMessage} style={{ marginBottom: 16 }} />
        ) : null}

        {taskId && startError ? (
          <Alert
            type="warning"
            showIcon
            message="任务已创建，但自动研究未启动。"
            description={
              <Space direction="vertical" size={4}>
                <Text>{startError}</Text>
                <Space wrap>
                  <Button size="small" type="primary" loading={starting} onClick={retryStart}>
                    重新启动研究
                  </Button>
                  <Button size="small" onClick={() => onCreated(taskId)}>
                    查看任务
                  </Button>
                </Space>
              </Space>
            }
            style={{ marginBottom: 16 }}
          />
        ) : null}

        <Button type="primary" htmlType="submit" loading={starting}>
          {submitLabel}
        </Button>
      </Form>
    </Card>
  );
}
