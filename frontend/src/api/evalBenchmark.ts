/** 评估 benchmark 摘要 API（后端 /app/api/v1/routes/eval_benchmark.py，只读）。 */

import { apiRequest } from './client';

export type BenchmarkRunMode = 'fake' | 'real';

export interface BenchmarkMetricRecord {
  status: string;
  value: string | null;
  numerator?: string | null;
  denominator?: string | null;
  reason_code?: string | null;
}

export interface BenchmarkAttemptRecord {
  dataset_id: string;
  dataset_version: number;
  as_of: string;
  case_id: string;
  variant_id: string;
  attempt_no: number;
  mode: string;
  status: 'success' | 'failed';
  error_code: string | null;
  wall_latency_ms: number | null;
  execution_id: string;
  variant_output_fingerprint: string | null;
  usage_components: string[];
  usage_call_count: number;
  total_tokens: number | null;
  estimated_cost_usd: string | null;
  citation_validity: BenchmarkMetricRecord | null;
  citation_coverage: BenchmarkMetricRecord | null;
  persisted: boolean;
  expected_fail_fast: boolean;
  notes: string[];
}

export interface BenchmarkSummaryPayload {
  dataset_id: string;
  dataset_version: number;
  as_of: string;
  mode: string;
  model: string;
  generated_at: string;
  attempts: BenchmarkAttemptRecord[];
}

export const evalBenchmarkKeys = {
  all: ['eval-benchmark'] as const,
  summary: (run: BenchmarkRunMode) => [...evalBenchmarkKeys.all, 'summary', run] as const,
};

export async function getEvalBenchmarkSummary(
  run: BenchmarkRunMode = 'fake',
): Promise<BenchmarkSummaryPayload> {
  return apiRequest<BenchmarkSummaryPayload>(`/eval/benchmark/summary?run=${run}`);
}
