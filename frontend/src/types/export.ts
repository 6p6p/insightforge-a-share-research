/** 确定性报告导出 API 类型（后端 /app/schemas/export.py，stage 6C spec P/Q）。

导出是**确定性导出**（0 LLM / 0 Retrieval / 0 Chroma / 0 Web）：POST 创建/
replay，GET metadata，content 端点下载字节（Content-Disposition attachment）。
下载字节不塞 JSON，走独立 content 端点。
 */

export type ExportFormat = 'markdown' | 'docx' | 'pdf';

export interface ExportCreateResponse {
  export_id: string;
  format: string;
  file_name: string;
  media_type: string;
  byte_size: number;
  /** 同输入（指纹相同）→ replay 已有行（POST 200），否则新建（201）。 */
  replayed: boolean;
  created_at: string;
}

export interface ExportMetadataResponse {
  export_id: string;
  task_id: string;
  report_id: string;
  format: string;
  file_name: string;
  media_type: string;
  byte_size: number;
  content_sha256: string;
  created_at: string;
}
