/** artifact 完整性错误检测与文案（Stage 6B.1 spec D/N）。

后端在锚定 checkpoint 引用某 artifact ID 但 verify_*_integrity 重建失败时抛
`TaskArtifactIntegrityError`（HTTP 409，code=`task_artifact_integrity`，统一
`{error:{code,message,request_id}}` 信封）。前端据此显示专用文案
「产物完整性校验失败」，其余错误按 ApiError.message 或兜底文案展示。
 */

import { ApiError } from '../../types/api';

export const INTEGRITY_ERROR_CODE = 'task_artifact_integrity';
export const INTEGRITY_ERROR_MESSAGE = '产物完整性校验失败';

export function isIntegrityError(error: unknown): boolean {
  return error instanceof ApiError && error.code === INTEGRITY_ERROR_CODE;
}

/** 返回 artifact tab 错误文案：完整性失败用专用文案，否则 ApiError.message 或兜底。 */
export function artifactErrorMessage(error: unknown, fallback: string): string {
  if (isIntegrityError(error)) {
    return INTEGRITY_ERROR_MESSAGE;
  }
  return error instanceof ApiError ? error.message : fallback;
}
