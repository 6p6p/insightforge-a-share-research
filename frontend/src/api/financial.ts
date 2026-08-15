/** 财务数据 API（后端 POST /tasks/{task_id}/financial-observations）。

用户手动录入的财务观测：从官方报告转录数字，创建 metric observation +
证据卡（evidence card）。
 */

import { apiRequest } from './client';
import type {
  FinancialObservationRequest,
  FinancialObservationResponse,
} from '../types/financial';

/** 创建用户录入的财务观测（201 证据卡已创建 / 422 校验失败如数字与引文不一致）。 */
export async function createUserSuppliedFinancialObservation(
  taskId: string,
  payload: FinancialObservationRequest,
): Promise<FinancialObservationResponse> {
  return apiRequest<FinancialObservationResponse>(
    `/tasks/${taskId}/financial-observations`,
    { method: 'POST', body: payload },
  );
}
