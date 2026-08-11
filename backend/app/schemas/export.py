"""Pydantic contracts for deterministic report export API (stage 6C spec P).

角色边界（Export 是**确定性导出**，不是 LLM 判断）：
- 契约只承载 metadata（export_id / format / file_name / media_type / byte_size /
  content_sha256 / created_at）；下载字节走 content 端点（Content-Disposition
  attachment，正确 MIME），不把字节塞进 JSON；
- `replayed`：同输入（export_input_fingerprint 相同）→ replay 已有行（POST 200 +
  `X-Export-Replayed: true`），否则新建（201 + `X-Export-Replayed: false`）。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ExportFormat = Literal["markdown", "docx", "pdf"]


class ExportCreateRequest(BaseModel):
    format: ExportFormat = "markdown"


class ExportCreateResponse(BaseModel):
    export_id: UUID
    format: str
    file_name: str
    media_type: str
    byte_size: int
    replayed: bool
    created_at: datetime


class ExportMetadataResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    export_id: UUID
    task_id: UUID
    report_id: UUID
    format: str
    file_name: str
    media_type: str
    byte_size: int
    content_sha256: str
    created_at: datetime
