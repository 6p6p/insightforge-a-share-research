/** 模型配置中心（v1.2.7-B）：查看/添加/编辑/删除模型配置，测试连接，设置当前模型。
 * 替代原「评估 Benchmark」页面。API Key 保存后不在响应出现明文。
 */

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  Layout,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';

import {
  createLlmConfig,
  deleteLlmConfig,
  listLlmConfigs,
  llmConfigKeys,
  setActiveLlmConfig,
  testLlmConnection,
  updateLlmConfig,
  type LlmConfigUpsertPayload,
  type LlmProviderConfigItem,
  type LlmProviderType,
} from '../api/llmProviderConfig';
import { PageTitle } from '../components/PageTitle';

const PROVIDER_OPTIONS: LlmProviderType[] = ['deepseek', 'openai', 'openrouter', 'custom'];

function defaultBaseUrl(provider: LlmProviderType): string {
  switch (provider) {
    case 'deepseek':
      return 'https://api.deepseek.com/v1';
    case 'openai':
      return 'https://api.openai.com/v1';
    case 'openrouter':
      return 'https://openrouter.ai/api/v1';
    default:
      return '';
  }
}

function initialValues(
  edit: LlmProviderConfigItem | undefined,
  fallbackBaseUrl: string,
): Record<string, unknown> {
  if (edit) {
    return {
      provider: edit.provider,
      display_name: edit.display_name,
      model_id: edit.model_id,
      base_url: edit.base_url ?? fallbackBaseUrl,
    };
  }
  return {
    provider: 'deepseek',
    base_url: defaultBaseUrl('deepseek'),
  };
}

interface EditorState {
  open: boolean;
  edit?: LlmProviderConfigItem;
}

export function ModelConfigPage(): React.JSX.Element {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: llmConfigKeys.list(),
    queryFn: () => listLlmConfigs(),
    retry: false,
  });

  const [editor, setEditor] = useState<EditorState>({ open: false });

  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: llmConfigKeys.all });
  };

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteLlmConfig(id),
    onSuccess: () => {
      message.success('「模型配置已删除」');
      invalidate();
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '删除模型配置失败');
    },
  });

  const activeMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      setActiveLlmConfig(id, active),
    onSuccess: () => {
      message.success('已设为当前使用模型');
      invalidate();
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '设置失败');
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => testLlmConnection(id),
    onSuccess: (result) => {
      if (result.ok) {
        message.success(`连接成功（${result.latency_ms ?? '-'}ms）：${result.message}`);
      } else {
        message.warning(result.message);
      }
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '测试连接失败');
    },
  });

  const items = data?.items ?? [];
  const activeId = data?.active_id ?? null;

  const columns: ColumnsType<LlmProviderConfigItem> = [
    { title: 'Provider', dataIndex: 'provider', width: 120 },
    { title: 'Display Name', dataIndex: 'display_name', width: 140 },
    { title: 'Model ID', dataIndex: 'model_id', width: 200 },
    {
      title: 'Base URL',
      dataIndex: 'base_url',
      render: (v: string | null) => (v ?? '-'),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 100,
      render: (active: boolean) =>
        active ? (
          <Typography.Text type="success">使用中</Typography.Text>
        ) : (
          <Typography.Text type="secondary">未启用</Typography.Text>
        ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 260,
      render: (_, record) => (
        <Space direction="vertical" size={2}>
          <Button
            type="link"
            size="small"
            onClick={() => setEditor({ open: true, edit: record })}
          >
            编辑
          </Button>
          <Button
            type="link"
            size="small"
            loading={testMutation.isPending}
            onClick={() => testMutation.mutate(record.id)}
          >
            测试连接
          </Button>
          {!record.is_active && (
            <Button
              type="link"
              size="small"
              onClick={() => activeMutation.mutate({ id: record.id, active: true })}
            >
              设为当前模型
            </Button>
          )}
          {activeId !== record.id && (
            <Popconfirm
              title="删除模型配置"
              description="确认删除该模型配置？删除后无法恢复。"
              okText="确认删除"
              cancelText="取消"
              okButtonProps={{ danger: true, loading: deleteMutation.isPending }}
              onConfirm={() => deleteMutation.mutate(record.id)}
            >
              <Button type="link" danger size="small">
                删除
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Layout.Content style={{ padding: 24 }}>
      <PageTitle title="模型配置中心" />
      {isError && (
        <Alert
          type="warning"
          showIcon
          message="无法读取模型配置"
          description={error instanceof Error ? error.message : undefined}
          style={{ marginBottom: 16 }}
        />
      )}
      <Card
        title="已配置模型"
        extra={
          <Button type="primary" onClick={() => setEditor({ open: true })}>
            添加模型配置
          </Button>
        }
      >
        <Table<LlmProviderConfigItem>
          rowKey="id"
          loading={isLoading}
          dataSource={items}
          columns={columns}
          pagination={false}
          locale={{
            emptyText: (
              <Typography.Text type="secondary">
                暂无模型配置，请点击右上角添加
              </Typography.Text>
            ),
          }}
        />
      </Card>
      <ConfigEditor
        open={editor.open}
        edit={editor.edit}
        onCancel={() => setEditor({ open: false })}
        onSaved={() => {
          setEditor({ open: false });
          invalidate();
        }}
      />
    </Layout.Content>
  );
}

interface ConfigEditorProps {
  open: boolean;
  edit?: LlmProviderConfigItem;
  onCancel: () => void;
  onSaved: () => void;
}

function ConfigEditor({
  open,
  edit,
  onCancel,
  onSaved,
}: ConfigEditorProps): React.JSX.Element {
  const [form] = Form.useForm();

  const submitMutation = useMutation({
    mutationFn: (payload: LlmConfigUpsertPayload) =>
      edit ? updateLlmConfig(edit.id, payload) : createLlmConfig(payload),
    onSuccess: () => {
      message.success(edit ? '模型配置已更新' : '模型配置已添加');
      onSaved();
    },
    onError: (error: unknown) => {
      message.error(error instanceof Error ? error.message : '保存失败');
    },
  });

  const handleSubmit = (values: LlmConfigUpsertPayload): void => {
    const { provider, display_name, model_id, base_url, api_key } = values;
    const payload: LlmConfigUpsertPayload = {
      provider,
      display_name,
      model_id,
      base_url: base_url?.trim() || null,
    };
    if (api_key !== undefined && api_key !== '') {
      payload.api_key = api_key;
    }
    if (!edit) {
      payload.is_active = false;
    }
    submitMutation.mutate(payload);
  };

  return (
    <Modal
      open={open}
      title={edit ? '编辑模型配置' : '添加模型配置'}
      destroyOnHidden
      onCancel={onCancel}
      onOk={() => form?.submit()}
      confirmLoading={submitMutation.isPending}
      okText="保存"
      cancelText="取消"
      width={580}
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={handleSubmit}
        initialValues={initialValues(edit ?? undefined, defaultBaseUrl(edit?.provider ?? 'deepseek'))}
      >
        <Form.Item
          name="provider"
          label="Provider"
          rules={[{ required: true, message: '请选择 Provider' }]}
        >
          <Select>
            {PROVIDER_OPTIONS.map((p) => (
              <Select.Option key={p} value={p}>
                {p}
              </Select.Option>
            ))}
          </Select>
        </Form.Item>
        <Form.Item
          name="display_name"
          label="Display Name（显示名）"
          rules={[{ required: true, message: '请输入显示名' }]}
        >
          <Input placeholder="例如：DeepSeek 主模型" />
        </Form.Item>
        <Form.Item
          name="model_id"
          label="Model ID"
          rules={[{ required: true, message: '请输入 Model ID' }]}
        >
          <Input placeholder="例如：deepseek-v4-flash" />
        </Form.Item>
        <Form.Item name="base_url" label="API Base URL">
          <Input placeholder="https://api.deepseek.com/v1" />
        </Form.Item>
        <Form.Item
          name="api_key"
          label="API Key"
          extra={
            edit && edit.has_api_key
              ? '已保存 API Key（保存后不可明文显示）；留空保持不变'
              : '保存后不可明文显示'
          }
          rules={edit ? [] : [{ required: true, message: '请输入 API Key' }]}
        >
          <Input.Password placeholder="sk-..." autoComplete="new-password" />
        </Form.Item>
        <Alert
          type="info"
          showIcon
          message="当前仅沿用 DeepSeek（研究执行路径认证 provider）。OpenAI / OpenRouter / Custom 配置可用于测试连接与展示，但研究流程仍走 .env 默认。"
        />
      </Form>
    </Modal>
  );
}
