/** 自动研究状态横幅（V1.1 产品语义）。

- 研究进行中：显示当前阶段（产品中文语义，不暴露后端 phase/status 枚举）；
- 资料不足（waiting_manual / 可 resume 的 research_backflow）：提供「上传 PDF」
  与「导入官方 URL」（复用既有 source-records 能力），成功后「继续研究」；
- 需要更新结构化数据：明确告知该缺口不能通过补文档解决；
- awaiting_stage5：人工审核决策（批准/重写/补充研究/取消）。

技术细节（error_code / need codes / attempt 等）折叠进「技术详情」，
默认 UI 回答三件事：发生了什么、为什么、用户下一步可以做什么。
 */

import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Form,
  Input,
  Select,
  Space,
  Tabs,
  Typography,
  Upload,
} from 'antd';
import type { FormInstance } from 'antd';
import { UploadOutlined } from '@ant-design/icons';

import {
  actOnBackflowReview,
  actOnOrchestration,
  getBackflowReview,
  orchestrationKeys,
  resumeSourceAcquisition,
} from '../../api/orchestrations';
import type { BackflowReview } from '../../types/orchestration';
import {
  importUrlSource,
  listSourceProviders,
  resolveProvider,
  sourceKeys,
  uploadSourceFile,
} from '../../api/sources';
import { taskKeys } from '../../api/tasks';
import { ApiError } from '../../types/api';
import {
  type OrchestrationAction,
  type OrchestrationPhase,
  type ResearchOrchestrationResponse,
  RESUME_MANUAL_REASONS,
  STRUCTURED_DATA_REFRESH_REASON,
} from '../../types/orchestration';
import {
  CONTROLLED_DOCUMENT_TYPES,
  SOURCE_DOCUMENT_TYPE_LABELS,
  type SourceDocumentType,
} from '../../types/source';
import { needCodeLabel } from '../../utils/needCode';
import { FinancialObservationForm } from '../financial/FinancialObservationForm';

const { Text } = Typography;

/** 阶段 → 产品语义（不暴露后端枚举；Final AUTO 模式研究进度）。 */
const PHASE_LABELS: Record<OrchestrationPhase, string> = {
  planning: '正在解析公司并规划研究',
  routing: '正在规划资料获取路线',
  preparing: '正在准备资料',
  fulfilling: '正在获取资料并提取财务数据',
  stage4: '正在智能分析',
  stage5: '正在生成报告',
  research_backflow: '正在补充研究',
  waiting_manual: '等待补充资料',
  awaiting_stage5: '等待人工确认',
  completed: '研究完成',
};

const STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  running: '进行中',
  waiting_human: '等待人工介入',
  completed: '研究完成',
  completed_with_warnings: '研究完成（包含审核提醒）',
  failed: '已失败',
  cancelled: '已取消',
};

/** manual_reason / error_code → 产品语义。 */
const MANUAL_REASON_LABELS: Record<string, string> = {
  source_acquisition_required: '需要补充资料',
  structured_data_refresh_required: '需要更新结构化数据',
  research_backflow_limit_reached: '自动补充研究已达到上限，需要人工确认',
  research_backflow_no_progress: '未能获取新的补充资料，需要人工确认',
  index_not_ready: '资料尚未处理完成，请稍后重试或重新补充',
  evidence_not_extracted: '资料已入库但未能提取证据，需要人工确认',
  report_audit_unavailable: '报告已生成但审计未通过校验，需要人工确认',
  report_audit_model_unavailable: '报告已生成但审计模型暂不可用，需要人工确认',
  report_audit_malformed_output: '报告已生成但审计输出无效，需要人工确认',
};

function reasonLabel(reason: string | null | undefined): string {
  if (!reason) {
    return '需要人工确认';
  }
  return MANUAL_REASON_LABELS[reason] ?? '需要人工确认';
}

/** 缺失需求：产品术语为主文本，原始 need code 附在括号内（技术详情行）。 */
function missingNeedsText(codes: string[]): string {
  if (codes.length === 0) {
    return '';
  }
  return `缺失需求：${codes.map(needCodeLabel).join('、')}（原始代码：${codes.join('、')}）`;
}

const STAGE5_ACTIONS: {
  action: OrchestrationAction;
  label: string;
  danger?: boolean;
  primary?: boolean;
}[] = [
  { action: 'approve', label: '接受报告', primary: true },
  { action: 'rewrite', label: '要求重写' },
  { action: 'research', label: '再次补充研究' },
  { action: 'cancel', label: '取消研究', danger: true },
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

  if (status === 'completed' || status === 'completed_with_warnings') {
    return (
      <Alert
        type={status === 'completed_with_warnings' ? 'warning' : 'success'}
        showIcon
        message="研究完成"
        description={
          status === 'completed_with_warnings'
            ? '报告已生成（包含审核提醒）——审核提醒已保留在报告中供参考，不影响报告交付；请在「报告」标签页查看各章节的说明与审核详情。'
            : '报告已生成，可在「报告」标签页查看并导出。'
        }
      />
    );
  }

  if (status === 'failed' || status === 'cancelled') {
    return (
      <Alert
        type="error"
        showIcon
        message={status === 'failed' ? '研究执行失败' : '研究已取消'}
        description={
          <Space direction="vertical" size={4}>
            <Text>{orchestration.error_message ?? '无错误信息'}</Text>
            {orchestration.error_code ? (
              <Collapse
                ghost
                size="small"
                items={[
                  {
                    key: 'detail',
                    label: '技术详情',
                    children: <Text type="secondary">错误代码：{orchestration.error_code}</Text>,
                  },
                ]}
              />
            ) : null}
          </Space>
        }
      />
    );
  }

  const needsSource =
    current_phase === 'waiting_manual' ||
    (current_phase === 'research_backflow' &&
      orchestration.manual_reason != null &&
      RESUME_MANUAL_REASONS.includes(orchestration.manual_reason));

  // 结构化数据补充缺口不在自动文档补充研究范围：上传 PDF / URL 不能解决。
  const structuredGap =
    current_phase === 'research_backflow' &&
    orchestration.manual_reason === STRUCTURED_DATA_REFRESH_REASON;

  const awaitingStage5 = status === 'waiting_human' && current_phase === 'awaiting_stage5';

  /** P0：backflow 上限/无进展 → 人工闭环（接受 / 再次补充研究 / 取消）。 */
  const backflowClosure =
    status === 'waiting_human' &&
    current_phase === 'research_backflow' &&
    !needsSource &&
    !structuredGap;

  const phaseLabel = PHASE_LABELS[current_phase] ?? current_phase;
  const statusLabel = STATUS_LABELS[status] ?? status;

  return (
    <Card title="自动研究" style={{ marginBottom: 16 }}>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Descriptions size="small" column={3}>
          <Descriptions.Item label="状态">
            <Text>{statusLabel}</Text>
          </Descriptions.Item>
          <Descriptions.Item label="当前阶段">{phaseLabel}</Descriptions.Item>
          {orchestration.backflow_round > 0 ? (
            <Descriptions.Item label="补充研究轮次">
              {orchestration.backflow_round}
            </Descriptions.Item>
          ) : null}
        </Descriptions>

        {needsSource ? (
          <SourceAcquisitionPanel orchestration={orchestration} companyId={companyId} />
        ) : null}

        {structuredGap ? (
          <Alert
            type="warning"
            showIcon
            message="需要更新结构化数据"
            description={
              <Space direction="vertical" size={4}>
                <div>自动补充研究已尽力完成文档类资料缺口；该估值数据缺口不在自动补充研究范围内（需重新计算估值数据），可人工补充数据或稍后刷新。</div>
                <Collapse
                  ghost
                  size="small"
                  items={[
                    {
                      key: 'detail',
                      label: '技术详情',
                      children: (
                        <Text type="secondary">
                          原因：{reasonLabel(orchestration.manual_reason)}
                          {missingNeedsText(orchestration.missing_need_codes)
                            ? `；${missingNeedsText(orchestration.missing_need_codes)}`
                            : ''}
                        </Text>
                      ),
                    },
                  ]}
                />
              </Space>
            }
          />
        ) : null}

        {awaitingStage5 ? (
          <OrchestrationHumanActionCard orchestration={orchestration} />
        ) : null}

        {backflowClosure ? (
          <BackflowClosureCard orchestration={orchestration} companyId={companyId} />
        ) : null}

        {status === 'waiting_human' &&
        !needsSource &&
        !structuredGap &&
        !awaitingStage5 &&
        !backflowClosure ? (
          <Alert
            type="warning"
            showIcon
            message="研究已暂停，等待人工确认"
            description={
              <Space direction="vertical" size={4}>
                <Text>{reasonLabel(orchestration.manual_reason)}</Text>
                {orchestration.manual_reason ? (
                  <Collapse
                    ghost
                    size="small"
                    items={[
                      {
                        key: 'detail',
                        label: '技术详情',
                        children: (
                          <Text type="secondary">原因：{orchestration.manual_reason}</Text>
                        ),
                      },
                    ]}
                  />
                ) : null}
              </Space>
            }
          />
        ) : null}

        {status !== 'waiting_human' && !needsSource && !awaitingStage5 ? (
          <Alert type="info" showIcon message={`自动研究进行中：${phaseLabel}`} />
        ) : null}

        {orchestration.attempt_no > 1 ? (
          <Collapse
            ghost
            size="small"
            items={[
              {
                key: 'detail',
                label: '技术详情',
                children: (
                  <Text type="secondary">尝试次数：# {orchestration.attempt_no}</Text>
                ),
              },
            ]}
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

interface SourceUrlFieldProps {
  form: FormInstance;
  label: string;
  placeholder?: string;
  /** 必填（受控 URL 导入）；上传表单 URL 可选。 */
  required?: boolean;
  companyId: string;
  providerOptions: { value: string; label: string }[];
}

/** 原始链接输入 + 「自动识别来源」：解析成功自动填入来源机构（可继续手动改）。 */
function SourceUrlField({
  form,
  label,
  placeholder,
  required = false,
  companyId,
  providerOptions,
}: SourceUrlFieldProps): React.JSX.Element {
  const url = Form.useWatch('source_url', form);
  const [hint, setHint] = useState<{ tone: 'success' | 'error'; text: string } | null>(null);
  const [resolving, setResolving] = useState(false);

  const resolve = async (): Promise<void> => {
    const trimmed = (url ?? '').trim();
    if (!trimmed) {
      setHint({ tone: 'error', text: '请先输入原始链接' });
      return;
    }
    setResolving(true);
    setHint(null);
    try {
      const result = await resolveProvider(companyId, trimmed);
      if (providerOptions.some((option) => option.value === result.provider_key)) {
        form.setFieldValue('provider_key', result.provider_key);
        setHint({ tone: 'success', text: `已识别来源：${result.display_name}（自动匹配）` });
      } else {
        setHint({ tone: 'error', text: '未能自动识别来源，请手动选择来源机构' });
      }
    } catch {
      setHint({ tone: 'error', text: '未能自动识别来源，请手动选择来源机构' });
    } finally {
      setResolving(false);
    }
  };

  return (
    <>
      <Form.Item
        name="source_url"
        label={label}
        rules={required ? [{ required: true, message: '请输入原始链接' }] : undefined}
      >
        <Input maxLength={2000} placeholder={placeholder} />
      </Form.Item>
      <Space size="small" style={{ marginBottom: 24 }}>
        <Button size="small" loading={resolving} onClick={() => void resolve()}>
          自动识别来源
        </Button>
        {hint ? (
          hint.tone === 'success' ? (
            <Text type="success" style={{ fontSize: 12 }}>{hint.text}</Text>
          ) : (
            <Text type="warning" style={{ fontSize: 12 }}>{hint.text}</Text>
          )
        ) : null}
      </Space>
    </>
  );
}

/** 资料不足面板：原因 + 上传 PDF / 受控 URL 导入 → 继续研究。 */
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
        source_url: values.source_url?.trim() || null,
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
  const errorMessage =
    mutationError instanceof ApiError
      ? mutationError.status === 413
        ? '文件过大：单个 PDF 不能超过 100MB'
        : mutationError.message
      : null;

  const providerOptions =
    providersQuery.data?.items.map((provider) => ({
      value: provider.provider_key,
      label: `${provider.display_name}（${provider.provider_key}）`,
    })) ?? [];

  const documentOptions = CONTROLLED_DOCUMENT_TYPES.map((type) => ({
    value: type,
    label: SOURCE_DOCUMENT_TYPE_LABELS[type],
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
            <Space direction="vertical" size={4}>
              <div>原因：{reasonLabel(orchestration.manual_reason)}</div>
              <div>请上传公司披露文件（如年报 PDF），或导入官方披露链接。资料处理完成后即可继续研究。</div>
              {orchestration.missing_need_codes.length > 0 ? (
                <Collapse
                  ghost
                  size="small"
                  items={[
                    {
                      key: 'detail',
                      label: '技术详情',
                      children: (
                        <Text type="secondary">
                          {missingNeedsText(orchestration.missing_need_codes)}
                        </Text>
                      ),
                    },
                  ]}
                />
              ) : null}
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
                      <Input maxLength={500} placeholder="例如：宁德时代 2023 年年度报告" />
                    </Form.Item>
                    <SourceUrlField
                      form={uploadForm}
                      label="原始链接（可选，官方披露链接）"
                      placeholder="https://static.szse.cn/…"
                      companyId={companyId}
                      providerOptions={providerOptions}
                    />
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
                label: '导入官方链接',
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
                      <Input maxLength={500} placeholder="例如：宁德时代 2023 年年度报告" />
                    </Form.Item>
                    <SourceUrlField
                      form={importForm}
                      label="官方 PDF 链接（受控域名）"
                      placeholder="https://static.szse.cn/…/annual.pdf"
                      required
                      companyId={companyId}
                      providerOptions={providerOptions}
                    />
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
              message={mode === 'upload' ? 'PDF 已保存并开始处理' : '官方链接已导入并开始处理'}
              description="资料处理完成后即可继续研究。"
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

// ------------------------------------------------------------------ 人工审核决策

/** awaiting_stage5：报告审核决策（继续顶层编排）。 */
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
      const message =
        error instanceof ApiError ? error.message : error instanceof Error ? error.message : null;
      setConflict(message);
      // v1.2.4 polish：任何错误（含 409 阻断性拒绝）都强制刷新——
      // 后端同步投影后（如 approval rejected → failed）UI 立即跟上，
      // 不再「第一次点击无反馈、第二次点击报已结束」。
      void queryClient.invalidateQueries({ queryKey: orchestrationKeys.all });
      void queryClient.invalidateQueries({ queryKey: taskKeys.workspace(orchestration.task_id) });
    },
  });

  const submitting = mutation.isPending;

  return (
    <Card title="需要人工确认" type="inner" size="small">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert
          type="warning"
          showIcon
          message="报告已生成，请确认研究结果"
          description="如发现问题，仍可要求重写、补充研究或取消；批准后报告将作为最终结果交付，审核提醒会保留在报告中。"
        />
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

// ------------------------------------------------------------------ P0 backflow 人工闭环

/** 可选面板中用户附带的证据（文件 / 原始链接 + 自动推导的上传所需字段）。 */
interface PendingEvidence {
  file: File | null;
  url: string;
  /** 自动默认第一个启用来源机构（尽力而为上传需要 provider_key）。 */
  providerKey: string;
  title: string;
}

const EMPTY_EVIDENCE: PendingEvidence = {
  file: null,
  url: '',
  providerKey: '',
  title: '',
};

/** 补充资料（可选）面板：复用既有 uploadSourceFile / importUrlSource 能力。只捕获，
 *  不留存动作；随后的「再次补充研究」动作会先用它尽力上传/导入（best-effort）。 */
function BackflowSupplementPanel({
  companyId,
  evidence,
  onEvidenceChange,
}: {
  companyId: string | null;
  evidence: PendingEvidence;
  onEvidenceChange: (e: PendingEvidence) => void;
}): React.JSX.Element {
  const providersQuery = useQuery({
    queryKey: sourceKeys.providers(),
    queryFn: () => listSourceProviders({ enabledOnly: true }),
    staleTime: 60_000,
  });
  const providerOptions =
    providersQuery.data?.items.map((provider) => ({
      value: provider.provider_key,
      label: `${provider.display_name}（${provider.provider_key}）`,
    })) ?? [];

  // 自动默认来源机构，减少用户选择负担（上传/导入需要 provider_key）。
  useEffect(() => {
    if (!evidence.providerKey && providerOptions.length > 0) {
      onEvidenceChange({ ...evidence, providerKey: providerOptions[0].value });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerOptions, evidence.providerKey]);

  if (!companyId) {
    return (
      <Alert type="warning" showIcon message="公司尚未解析，暂无法补充资料" />
    );
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="small">
      <Text type="secondary">
        可附加证据文件与官方链接，供交叉验证；留空则继续全自动补充研究。
      </Text>
      <Upload
        accept=".pdf,application/pdf"
        maxCount={1}
        beforeUpload={(f) => {
          onEvidenceChange({ ...evidence, file: f as File });
          return false;
        }}
        onRemove={() => onEvidenceChange({ ...evidence, file: null })}
        fileList={evidence.file ? [{ uid: evidence.file.name, name: evidence.file.name }] : []}
      >
        <Button icon={<UploadOutlined />}>选择附加文件（可选）</Button>
      </Upload>
      <Input
        placeholder="原始链接（可选，官方披露链接）"
        value={evidence.url}
        maxLength={2000}
        onChange={(e) => onEvidenceChange({ ...evidence, url: e.target.value })}
      />
    </Space>
  );
}

/** research_backflow 已达上限/无进展：人工闭环（接受 / 再次补充研究 / 取消）。 */

/** P0 修复：backflow 人工闭环头部消息按真实原因区分（audit 失败 vs 补研上限）。 */
function closureAlertMessage(reason: string | null): string {
  if (!reason) return '需要人工确认';
  if (reason.startsWith('report_audit_')) return '报告已生成但自动审核失败，需要重新验证';
  if (reason === 'research_backflow_limit_reached') return '自动补充研究已达到上限';
  if (reason === 'research_backflow_no_progress') return '未能获取新的补充资料';
  return MANUAL_REASON_LABELS[reason] ?? '需要人工确认';
}
function BackflowClosureCard({
  orchestration,
  companyId,
}: {
  orchestration: ResearchOrchestrationResponse;
  companyId: string | null;
}): React.JSX.Element {
  const queryClient = useQueryClient();
  const [evidence, setEvidence] = useState<PendingEvidence>(EMPTY_EVIDENCE);
  const reviewQuery = useQuery({
    queryKey: ['backflow-review', orchestration.orchestration_id],
    queryFn: () => getBackflowReview(orchestration.orchestration_id),
    enabled: orchestration.orchestration_id != null,
  });
  const review: BackflowReview | undefined = reviewQuery.data;

  const mutation = useMutation({
    mutationFn: (action: 'accept' | 'extra_research' | 'cancel') =>
      actOnBackflowReview(orchestration.orchestration_id, action),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: orchestrationKeys.all });
      void queryClient.invalidateQueries({
        queryKey: taskKeys.workspace(orchestration.task_id),
      });
      void queryClient.invalidateQueries({
        queryKey: ['backflow-review', orchestration.orchestration_id],
      });
    },
    onError: (error: unknown) => {
      if (error instanceof ApiError) {
        void queryClient.invalidateQueries({ queryKey: orchestrationKeys.all });
        void queryClient.invalidateQueries({
          queryKey: taskKeys.workspace(orchestration.task_id),
        });
      }
    },
  });

  const barriers = review?.acceptance_barriers ?? [];
  // v1.2.5：内容审核问题不再阻断——只有系统级 barrier（无法定位报告 /
  // 审核记录未生成等）才禁用接受；impact scope 仅决定提醒文案与完成状态。
  const acceptDisabled = barriers.length > 0 || mutation.isPending;
  const submitting = mutation.isPending;
  const done = review?.decision != null;
  const impactScope: string | null = review?.impact_scope ?? null;
  /** v1.2.5：CRITICAL_ALERT（report_blocking 枚举值）→ 重要审核提醒；
   * WARNING（section_warning / section_unavailable）→ 审核提醒；均允许接受。 */
  const criticalAlert = !done && impactScope === 'report_blocking';
  const sectionWarning =
    !done &&
    impactScope != null &&
    impactScope !== 'report_blocking' &&
    impactScope !== 'info';

  /** 尽力而为：把用户在可选面板中附带的资料先上传/导入，失败也不阻断动作。 */
  const flushEvidenceBestEffort = async (): Promise<void> => {
    const { file, url, providerKey, title } = evidence;
    const trimmedUrl = url.trim();
    if (!file && !trimmedUrl) {
      return;
    }
    if (!companyId || !providerKey) {
      return;
    }
    try {
      if (file) {
        await uploadSourceFile({
          company_id: companyId,
          provider_key: providerKey,
          document_type: 'annual_report',
          title: title.trim() || file.name,
          source_url: trimmedUrl || null,
          file,
        });
      } else {
        await importUrlSource({
          company_id: companyId,
          provider_key: providerKey,
          document_type: 'annual_report',
          title: title.trim() || '补充资料',
          source_url: trimmedUrl,
        });
      }
    } catch {
      // best-effort：附加资料失败不阻断「再次补充研究」。
    }
  };

  /** 再次补充研究：先尽力附带可选资料，再触发与今天一致的 extra_research 动作。 */
  const handleExtraResearch = async (): Promise<void> => {
    await flushEvidenceBestEffort();
    mutation.mutate('extra_research');
  };

  return (
    <Card title="需要人工确认" type="inner" size="small">
      <Space direction="vertical" style={{ width: '100%' }}>
        <Alert
          type="warning"
          showIcon
          message={closureAlertMessage(review?.reason ?? orchestration.manual_reason)}
          description="你可以接受当前报告、再次补充研究（有界），或取消研究。"
        />
        {!done && acceptDisabled && barriers.length > 0 ? (
          <Alert
            type="error"
            showIcon
            message="当前报告暂不能接受"
            description={barriers.join('；')}
          />
        ) : null}
        {criticalAlert ? (
          <Alert
            type="warning"
            showIcon
            message="当前报告存在重要审核提醒"
            description="发现可能影响结论可靠性的因素，请查看审核详情后决定。"
          />
        ) : null}
        {sectionWarning ? (
          <Alert
            type="warning"
            showIcon
            message="当前报告存在审核提醒"
            description="部分章节存在缺口，但其他内容仍可查看与接受。"
          />
        ) : null}
        <Collapse
          size="small"
          defaultActiveKey={[]}
          items={[
            {
              key: 'supplement',
              label: '补充资料（可选 / 附加证据供交叉验证）',
              children: (
                <BackflowSupplementPanel
                  companyId={companyId}
                  evidence={evidence}
                  onEvidenceChange={setEvidence}
                />
              ),
            },
            {
              key: 'financial',
              label: '补充财务数据（可选）',
              children: (
                <FinancialObservationForm
                  taskId={orchestration.task_id}
                  companyId={companyId}
                />
              ),
            },
          ]}
        />
        <Space wrap>
          <Button
            type="primary"
            loading={submitting}
            disabled={acceptDisabled || done}
            onClick={() => mutation.mutate('accept')}
            data-action="accept"
          >
            接受当前报告
          </Button>
          <Button
            loading={submitting}
            disabled={submitting || done}
            onClick={() => void handleExtraResearch()}
            data-action="extra_research"
          >
            再次补充研究
          </Button>
          <Button
            danger
            loading={submitting}
            disabled={submitting || done}
            onClick={() => mutation.mutate('cancel')}
            data-action="cancel"
          >
            取消研究
          </Button>
        </Space>
        {done ? (
          <Alert type="success" showIcon message="已收到你的操作，正在处理。" />
        ) : null}
      </Space>
    </Card>
  );
}