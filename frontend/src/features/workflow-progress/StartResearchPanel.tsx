/** 启动研究执行面板（V1.1 产品语义）。

任务已创建但尚无进行中的研究时显示；提交 → 按手动研究方案启动研究。
已有进行中的研究（pending/running/waiting_human）时隐藏。
 */

import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Alert, Button, Card, Space } from 'antd';

import { executeTask } from '../../api/tasks';
import { ApiError } from '../../types/api';
import type { AnalysisWorkItem } from '../../types/workspace';
import { WorkPlanEditor } from '../task-create/WorkPlanEditor';

interface Props {
  taskId: string;
  onStarted?: () => void;
}

export function StartResearchPanel({ taskId, onStarted }: Props): React.JSX.Element {
  const [workItems, setWorkItems] = useState<AnalysisWorkItem[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => executeTask(taskId, { analysis_work_items: workItems }),
    onSuccess: () => {
      setErrorMessage(null);
      onStarted?.();
    },
    onError: (error: unknown) => {
      const message = error instanceof ApiError ? error.message : '启动失败，请重试';
      setErrorMessage(message);
      if (error instanceof ApiError && error.isConflict) {
        onStarted?.(); // 409：运行状态已变化 → 让上层刷新。
      }
    },
  });

  const disabled = workItems.length === 0 || mutation.isPending;

  return (
    <Card title="启动研究执行" type="inner">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="info"
          showIcon
          message="请填写研究方案：每条研究条目引用已入库的真实证据 / 计算 / 对比 ID。"
        />
        <WorkPlanEditor value={workItems} onChange={setWorkItems} disabled={mutation.isPending} />
        {errorMessage ? <Alert type="error" showIcon message={errorMessage} /> : null}
        <Button type="primary" loading={mutation.isPending} disabled={disabled} onClick={() => mutation.mutate()}>
          开始执行
        </Button>
      </Space>
    </Card>
  );
}
