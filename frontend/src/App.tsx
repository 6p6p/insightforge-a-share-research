/** 应用外壳：顶部导航 + 路由。 */

import { Layout, Menu } from 'antd';
import { Link } from 'react-router-dom';

import { AppRoutes } from './routes/AppRoutes';

const { Header } = Layout;

export function App(): React.JSX.Element {
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
        <span style={{ color: '#fff', fontSize: 18, fontWeight: 600 }}>InsightForge 研究台</span>
        <Menu
          theme="dark"
          mode="horizontal"
          selectable={false}
          items={[
            { key: 'tasks', label: <Link to="/tasks">任务列表</Link> },
            { key: 'new', label: <Link to="/tasks/new">新建任务</Link> },
          ]}
          style={{ flex: 1, minWidth: 0 }}
        />
      </Header>
      <AppRoutes />
    </Layout>
  );
}
