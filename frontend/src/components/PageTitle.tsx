/** 轻量页面标题（antd v5 移除 PageHeader 后的替代）。 */

import { Space, Typography } from 'antd';

const { Title, Text } = Typography;

interface Props {
  title: string;
  subTitle?: string;
  extra?: React.ReactNode;
}

export function PageTitle({ title, subTitle, extra }: Props): React.JSX.Element {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
      <div style={{ flex: 1 }}>
        <Title level={4} style={{ margin: 0 }}>
          {title}
        </Title>
        {subTitle ? <Text type="secondary">{subTitle}</Text> : null}
      </div>
      {extra ? <Space>{extra}</Space> : null}
    </div>
  );
}
