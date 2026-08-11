/** 与后端 /app/schemas/error.py 对齐的统一错误信封。 */
export interface ErrorDetail {
  code: string;
  message: string;
  request_id: string;
}

export interface ErrorEnvelope {
  error: ErrorDetail;
}

/** FastAPI 校验错误（422 未走 DomainError 时的 {detail: [...]} 形状）。 */
export type ValidationDetail = { loc: (string | number)[]; msg: string; type: string }[];

/** ApiError：API client 抛出的统一错误。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  /** 422 时的 FastAPI validation detail（可选）。 */
  readonly validation?: ValidationDetail;

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string,
    validation?: ValidationDetail,
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.validation = validation;
  }

  get isConflict(): boolean {
    return this.status === 409;
  }
}
