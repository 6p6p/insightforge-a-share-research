/** 顶层自动研究编排横幅（7A Product Gate spec N）。

- 编排运行中：显示当前 phase / status / attempt / backflow_round。
- `waiting_manual` 或 `research_backflow` + 可 resume 的 manual_reason：
  「资料不足」面板 —— 展示缺失 need codes + 原因，提供「上传 PDF」与
  「导入官方 URL」（复用既有 source-records 能力），成功后显示
  「继续研究」→ resume-source-acquisition（同 orchestration + 同顶层 thread）。
- `awaiting_stage5`：Stage 5 人工决策，复用现有 approve/rewrite/research/cancel
  文案，但 dispatch 到 /research-orchestrations/{id}/actions（继续顶层图）。
 */

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Select,
  Space,
  Tabs,
  Typography,
  Upload,
} from 'antd';
import { UploadOutlined } from '@ant-design/icons';

import {
  actOnOrchestration,
  orchestrationKeys,
  resumeSourceAcquisition,
} from '../../api/orchestrations';
import { importUrlSource, listSourceProviders, sourceKeys, uploadSourceFile } from '../../api/sources';
import { taskKeys } from '../../api/tasks';
import { ApiError } from '../../types/api';
import {
  type OrchestrationAction,
  type OrchestrationPhase,
  type ResearchOrchestrationResponse,
  RESUME_MANUAL_REASONS,
} from '../../types/orchestration';
import { CONTROLLED_DOCUMENT_TYPES, type SourceDocumentType } from '../../types/source';

const { Text } = Typography;

const PHASE_LABELS: Record<OrchestrationPhase, string> = {
  planning: '生成研究计划',
  routing: '规划分析路线',
  preparing: '准备资料',
  fulfilling: '补齐资料',
  stage4: 'Stage 4 分析',
  stage5: 'Stage 5 报告',
  research_backflow: '研究回填',
  waiting_manual: '等待补充资料',
  awaiting_stage5: '等待报告审核',
  completed: '已完成',
};

const DOCUMENT_TYPE_LABELS: Record<SourceDocumentType, string> = {
  annual_report: '年报',
  semiannual_report: '半年报',
  quarterly_report: '季报',
  company_announcement: '公司公告',
  issuer_ir_material: '发行人信披材料',
  prospectus: '招股书',
  news_article: '新闻文章',
  other: '其他',
};

const STAGE5_ACTIONS: {
  action: OrchestrationAction;
  label: string;
  danger?: boolean;
  primary?: boolean;
}[] = [
  { action: 'approve', label: '批准通过', primary: true },
  { action: 'rewrite', label: '要求重写' },
  { action: 'research', label: '需要补充研究' },
  { action: 'cancel', label: '取消执行', danger: true },
];

interface Props {
  orchestration: ResearchOrchestrationResponse | null;
  /** 上传/导入需要 company_id（来自 workspace resolved_company）。 */
  companyId: string | null;
}

export function OrchestrationBanner({
  orchestration,
  companyId,
}: Props): React.JSX.Element | null {
  if (!orchestration) {
    return null;
  }

  const { status, current_phase } = orchestration;

  if (status === 'completed') {
    return (
      <Alert
        type="success"
        showIcon
        message="自动研究已完成"
        description={`报告已生成（尝试 #${orchestration.attempt_no}）。`}
      />
    );
  }

  if (status === 'failed' || status === 'cancelled') {
    return (
      <Alert
        type="error"
        showIcon
        message={status === 'failed' ? '自动研究失败' : '自动研究已取消'}
        description={
          orchestration.error_message ?? orchestration.error_code ?? '无错误信息'
        }
      />
    );
  }

  const needsSource =
    current_phase === 'waiting_manual' ||
    (current_phase === 'research_backflow' &&
      orchestration.manual_reason != null &&
      RESUME_MANUAL_REASONS.includes(orchestration.manual_reason));

  const awaitingStage5 = status === 'waiting_human' && current_phase === 'awaiting_stage5';

  return (
    <Card title="自动研究编排" style={{ marginBottom: 16 }}>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="状态">
            <Text>{status === 'waiting_human' ? '等待人工介入' : status}</Text>
          </Descriptions.Item>
          <Descriptions.Item label="当前阶段">
            {PHASE_LABELS[current_phase] ?? current_phase}
          </Descriptions.Item>
          <Descriptions.Item label="尝试次数">#{orchestration.attempt_no}</Descriptions.Item>
          {orchestration.backflow_round > 0 ? (
            <Descriptions.Item label="回填轮次">
              {orchestration.backflow_round}
            </Descriptions.Item>
          ) : null}
        </Descriptions>

        {needsSource ? (
          <SourceAcquisitionPanel orchestration={orchestration} companyId={companyId} />
        ) : null}

        {awaitingStage5 ? (
          <OrchestrationHumanActionCard orchestration={orchestration} />
        ) : null}

        {status === 'waiting_human' && !needsSource && !awaitingStage5 ? (
          // 例如 research_backflow_limit_reached：顶层已暂停，但没有可 resume 的
          // 补资料路径，也没有 Stage 5 人工决策 → 明确告知（不可绕过 MAX rounds）。
          <Alert
            type="warning"
            showIcon
            message="研究已暂停，等待人工介入"
            description={orchestration.manual_reason ?? undefined}
          />
        ) : null}

        {status !== 'waiting_human' && !needsSource && !awaitingStage5 ? (
          <Alert
            type="info"
            showIcon
            message={`自动研究进行中：${PHASE_LABELS[current_phase] ?? current_phase}`}
          />
        ) : null}
      </Space>
    </Card>
  );
}

// ------------------------------------------------------------------ 补资料面板

interface AcquisitionPanelProps {
  orchestration: ResearchOrchestrationResponse;
  companyId: string | null;
}

type AcquisitionMode = 'none' | 'upload' | 'import';

interface UploadFormValues {
  provider_key: string;
  document_type: SourceDocumentType;
  title: string;
  source_url: string;
}

interface ImportFormValues {
  provider_key: string;
  document_type: SourceDocumentType;
  title: string;
  source_url: string;
}

/** 资料不足面板：need codes + 原因 + 上传 PDF / 受控 URL 导入 → 继续研究。 */
function SourceAcquisitionPanel({
  orchestration,
  companyId,
}: AcquisitionPanelProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<AcquisitionMode>('none');
  const [uploadForm] = Form.useForm<UploadFormValues>();
  const [importForm] = Form.useForm<ImportFormValues>();

  const providersQuery = useQuery({
    queryKey: sourceKeys.providers(),
    queryFn: () => listSourceProviders({ enabledOnly: true }),
    staleTime: 60_000,
  });

  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: orchestrationKeys.all });
    void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(orchestration.task_id) });
  };

  const uploadMutation = useMutation({
    mutationFn: (values: UploadFormValues) =>
      uploadSourceFile({
        company_id: companyId!,
        provider_key: values.provider_key,
        document_type: values.document_type,
        title: values.title.trim(),
        source_url: values.source_url.trim(),
        file: file!,
      }),
    onSuccess: () => {
      setMode('upload');
      uploadForm.resetFields();
    },
  });

  const importMutation = useMutation({
    mutationFn: (values: ImportFormValues) =>
      importUrlSource({
        company_id: companyId!,
        provider_key: values.provider_key,
        document_type: values.document_type,
        title: values.title.trim(),
        source_url: values.source_url.trim(),
      }),
    onSuccess: () => {
      setMode('import');
      importForm.resetFields();
    },
  });

  const resumeMutation = useMutation({
    mutationFn: () => resumeSourceAcquisition(orchestration.orchestration_id),
    onSuccess: invalidate,
    onError: (error: unknown) => {
      if (error instanceof ApiError && error.isConflict) {
        invalidate();
      }
    },
  });

  const mutationError =
    uploadMutation.error ?? importMutation.error ?? resumeMutation.error;
  const errorMessage = mutationError instanceof ApiError ? mutationError.message : null;

  const providerOptions =
    providersQuery.data?.items.map((provider) => ({
      value: provider.provider_key,
      label: `${provider.display_name}（${provider.provider_key}）`,
    })) ?? [];

  const documentOptions = CONTROLLED_DOCUMENT_TYPES.map((type) => ({
    value: type,
    label: DOCUMENT_TYPE_LABELS[type],
  }));

  if (!companyId) {
    return (
      <Alert
        type="warning"
        showIcon
        message="公司尚未解析，暂无法补充资料"
        description="请先在公司解析成功后重试。"
      />
    );
  }

  return (
    <Card title="研究资料不足" type="inner" size="small">
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          type="warning"
          showIcon
          message="研究需要的资料不完整"
          description={
            <Space direction="vertical" size={0}>
              <div>原因：{orchestration.manual_reason ?? '—'}</div>
              <div>
                缺失需求代码：
                {orchestration.missing_need_codes.length > 0
                  ? orchestration.missing_need_codes.join('、')
                  : '—'}
              </div>
            </Space>
          }
        />

        {mode === 'none' ? (
          <Tabs
            size="small"
            // 同一时刻只挂载一个表单：避免两个 Form 相同字段名产生重复 id，
            // 导致 label 绑定错乱（影响可访问性与测试）。
            destroyOnHidden
            items={[
              {
                key: 'upload',
                label: '上传 PDF',
                children: (
                  <Form<UploadFormValues>
                    form={uploadForm}
                    layout="vertical"
                    onFinish={(values) => uploadMutation.mutate(values)}
                    initialValues={{ document_type: 'annual_report' }}
                  >
                    <Form.Item label="PDF 文件" required>
                      <Upload
                        accept=".pdf,application/pdf"
                        maxCount={1}
                        beforeUpload={(f) => {
                          setFile(f as File);
                          return false;
                        }}
                        onRemove={() => setFile(null)}
                        fileList={file ? [{ uid: file.name, name: file.name }] : []}
                      >
                        <Button icon={<UploadOutlined />}>选择 PDF 文件</Button>
                      </Upload>
                    </Form.Item>
                    <Form.Item
                      name="provider_key"
                      label="来源机构"
                      rules={[{ required: true, message: '请选择来源机构' }]}
                    >
                      <Select
                        showSearch
                        placeholder="选择来源机构"
                        options={providerOptions}
                        loading={providersQuery.isLoading}
                        notFoundContent={providersQuery.isError ? '来源机构加载失败' : undefined}
                      />
                    </Form.Item>
                    <Form.Item
                      name="document_type"
                      label="文档类型"
                      rules={[{ required: true }]}
                    >
                      <Select options={documentOptions} />
                    </Form.Item>
                    <Form.Item
                      name="title"
                      label="标题"
                      rules={[{ required: true, message: '请输入文档标题' }]}
                    >
                      <Input maxLength={500} placeholder="例如：贵州茅台 2025 年年度报告" />
                    </Form.Item>
                    <Form.Item
                      name="source_url"
                      label="原始链接（必须在所选来源机构的受控域名内）"
                      rules={[{ required: true, message: '请输入原始链接' }]}
                    >
                      <Input maxLength={2000} placeholder="https://static.sse.com.cn/…" />
                    </Form.Item>
                    <Button
                      type="primary"
                      htmlType="submit"
                      loading={uploadMutation.isPending}
                      disabled={!file}
                    >
                      上传并保存
                    </Button>
                  </Form>
                ),
              },
              {
                key: 'import',
                label: '导入官方 URL',
                children: (
                  <Form<ImportFormValues>
                    form={importForm}
                    layout="vertical"
                    onFinish={(values) => importMutation.mutate(values)}
                    initialValues={{ document_type: 'annual_report' }}
                  >
                    <Form.Item
                      name="provider_key"
                      label="来源机构"
                      rules={[{ required: true, message: '请选择来源机构' }]}
                    >
                      <Select
                        showSearch
                        placeholder="选择来源机构"
                        options={providerOptions}
                        loading={providersQuery.isLoading}
                        notFoundContent={providersQuery.isError ? '来源机构加载失败' : undefined}
                      />
                    </Form.Item>
                    <Form.Item
                      name="document_type"
                      label="文档类型"
                      rules={[{ required: true }]}
                    >
                      <Select options={documentOptions} />
                    </Form.Item>
                    <Form.Item
                      name="title"
                      label="标题"
                      rules={[{ required: true, message: '请输入文档标题' }]}
                    >
                      <Input maxLength={500} placeholder="例如：贵州茅台 2025 年年度报告" />
                    </Form.Item>
                    <Form.Item
                      name="source_url"
                      label="官方 PDF 链接（受控域名）"
                      rules={[{ required: true, message: '请输入官方 PDF 链接' }]}
                    >
                      <Input maxLength={2000} placeholder="https://static.sse.com.cn/…/annual.pdf" />
                    </Form.Item>
                    <Button type="primary" htmlType="submit" loading={importMutation.isPending}>
                      导入并保存
                    </Button>
                  </Form>
                ),
              },
            ]}
          />
        ) : (
          <>
            <Alert
              type="success"
              showIcon
              message={mode === 'upload' ? 'PDF 已保存为研究来源' : '官方 URL 已导入为研究来源'}
            />
            <Button
              type="primary"
              loading={resumeMutation.isPending}
              disabled={resumeMutation.isPending}
              onClick={() => resumeMutation.mutate()}
            >
              继续研究
            </Button>
          </>
        )}

        {errorMessage ? <Alert type="error" showIcon message={errorMessage} /> : null}
      </Space>
    </Card>
  );
}

// ------------------------------------------------------------------ Stage 5 人工决策

/** awaiting_stage5：与 HumanActionCard 相同的操作文案，但 dispatch 到
 * /research-orchestrations/{id}/actions（继续顶层编排，而不是只 resume 子图）。 */
function OrchestrationHumanActionCard({
  orchestration,
}: {
  orchestration: ResearchOrchestrationResponse;
}): React.JSX.Element {
  const queryClient = useQueryClient();
  const [comment, setComment] = useState('');
  const [conflict, setConflict] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (action: OrchestrationAction) =>
      actOnOrchestration(orchestration.orchestration_id, action, comment.trim() || null),
    onSuccess: () => {
      setConflict(null);
      void queryClient.invalidateQueries({ queryKey: orchestrationKeys.all });
      void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(orchestration.task_id) });
    },
    onError: (error: unknown) => {
      if (error instanceof ApiError && error.isConflict) {
        setConflict(error.message);
        void queryClient.invalidateQueries({ queryKey: orchestrationKeys.all });
        void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(orchestration.task_id) });
      }
    },
  });

  const submitting = mutation.isPending;

  return (
    <Card title="需要人工确认" type="inner" size="small">
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
          {STAGE5_ACTIONS.map((action) => (
            <Button
              key={action.action}
              type={action.primary ? 'primary' : 'default'}
              danger={action.danger}
              loading={submitting}
              disabled={submitting}
              onClick={() => mutation.mutate(action.action)}
              data-action={action.action}
            >
              {action.label}
            </Button>
          ))}
        </Space>
      </Space>
    </Card>
  );
}
