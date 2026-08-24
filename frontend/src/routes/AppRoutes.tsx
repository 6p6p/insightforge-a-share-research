/** 应用路由。 */

import { Navigate, Route, Routes } from 'react-router-dom';

import { ModelConfigPage } from '../pages/ModelConfigPage';
import { TaskCreatePage } from '../pages/TaskCreatePage';
import { TaskListPage } from '../pages/TaskListPage';
import { TaskWorkspacePage } from '../pages/TaskWorkspacePage';

export function AppRoutes(): React.JSX.Element {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/tasks" replace />} />
      <Route path="/tasks" element={<TaskListPage />} />
      <Route path="/tasks/new" element={<TaskCreatePage />} />
      <Route path="/tasks/:taskId" element={<TaskWorkspacePage />} />
      <Route path="/models" element={<ModelConfigPage />} />
      <Route path="*" element={<Navigate to="/tasks" replace />} />
    </Routes>
  );
}
