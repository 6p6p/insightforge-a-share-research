/** Human Action UI（spec M）。

pending_action 出现时显示操作卡；按钮按真实 pending_action / graph_name 动态
出现。提交后禁用按钮直到服务器响应；409 时重新 fetch 状态、不假装成功。
 */

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Input, Space, Typography } from 'antd';

import { postRunAction } from '../../api/workflow';
import { ApiError } from '../../types/api';
import type { ActionType, WorkflowRunResponse } from '../../types/workflow';
import { taskKeys } from '../../api/tasks';
import { STAGE5_GRAPH_NAME } from '../../utils/stage5';

const { Text } = Typography;

interface ActionOption {
  type: ActionType;
  label: string;
  danger?: boolean;
  primary?: boolean;
}

/** 依据真实 pending_action 决定可用操作。 */
export function actionsForRun(run: WorkflowRunResponse): ActionOption[] {
  if (run.pending_action === 'human_review' || run.graph_name === STAGE5_GRAPH_NAME) {
    return [
      { type: 'approve', label: '批准通过', primary: true },
      { type: 'rewrite', label: '要求重写' },
      { type: 'research', label: '需要补充研究' },
      { type: 'cancel', label: '取消执行', danger: true },
    ];
  }
  return [
    { type: 'approve_plan', label: '批准计划', primary: true },
    { type: 'cancel', label: '取消', danger: true },
    { type: 'retry', label: '重试' },
  ];
}

interface Props {
  run: WorkflowRunResponse;
  /** 提交后回调（用于失效查询 / 刷新）。 */
  onSettled?: () => void;
}

export function HumanActionCard({ run, onSettled }: Props): React.JSX.Element {
  const queryClient = useQueryClient();
  const [comment, setComment] = useState('');
  const [conflict, setConflict] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (action: ActionType) =>
      postRunAction(run.run_id, { action_type: action, comment: comment.trim() || null }),
    onSuccess: () => {
      setConflict(null);
      void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(run.task_id) });
      void queryClient.invalidateQueries({ queryKey: ['workflow-runs'] });
      onSettled?.();
    },
    onError: (error: unknown) => {
      if (error instanceof ApiError && error.isConflict) {
        // 409：运行状态已变化 → 重新 fetch，不假装成功。
        setConflict(error.message);
        void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(run.task_id) });
        onSettled?.();
      }
    },
  });

  const actions = actionsForRun(run);
  const submitting = mutation.isPending;

  return (
    <Card
      title="需要人工确认"
      type="inner"
      extra={<Text type="secondary">待处理操作：{run.pending_action ?? '—'}</Text>}
    >
      <Space direction="vertical" style={{ width: '100%' }}>
        {conflict ? (
          <Alert type="warning" showIcon message="状态已变化" description={conflict} />
        ) : null}
        <Input.TextArea
          rows={2}
          placeholder="审批意见（可选）"
          value={comment}
          disabled={submitting}
          onChange={(e) => setComment(e.target.value)}
          aria-label="审批意见"
        />
        <Space wrap>
          {actions.map((action) => (
            <Button
              key={action.type}
              type={action.primary ? 'primary' : 'default'}
              danger={action.danger}
              loading={submitting}
              disabled={submitting}
              onClick={() => mutation.mutate(action.type)}
              data-action={action.type}
            >
              {action.label}
            </Button>
          ))}
        </Space>
      </Space>
    </Card>
  );
}
