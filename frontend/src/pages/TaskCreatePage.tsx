/** 新建研究任务页（spec J）。创建后跳转 /tasks/:taskId。 */

import { useNavigate } from 'react-router-dom';
import { Layout } from 'antd';

import { PageTitle } from '../components/PageTitle';
import { TaskCreateForm } from '../features/task-create/TaskCreateForm';

export function TaskCreatePage(): React.JSX.Element {
  const navigate = useNavigate();
  return (
    <Layout.Content style={{ padding: 24, maxWidth: 720 }}>
      <PageTitle title="新建研究任务" />
      <TaskCreateForm
        onCreated={(taskId) => {
          navigate(`/tasks/${taskId}`);
        }}
      />
    </Layout.Content>
  );
}
