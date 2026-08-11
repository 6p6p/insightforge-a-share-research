"""Deterministic report export contracts (stage 6C): pack + fingerprints + renderer registry.

角色边界（Export 是**确定性渲染**，不是 LLM 判断，spec F）：
- 0 LLM / 0 Retrieval / 0 Chroma / 0 Web——renderer 只消费已构建的
  `ExportReportPack` 纯结构，**不重写正文、不生成观点、不判断证据**；
- 确定性代码负责：canonical lineage 恢复 Verified Report / Check / Audit /
  HumanDecision → 资格判定（spec H）→ 引用编号（spec J，section_order →
  paragraph_index → evidence_card_ids 首次出现 → E1..En）→ 构建 ExportReportPack
  → `compute_export_input_fingerprint` → replay（同输入 → 同一行）→ renderer →
  内容寻址归档 → create_or_get 原子持久化 → verify_export_integrity（read-side）。

冻结常量：
- `EXPORT_SCHEMA_VERSION = 1`（report_exports.export_schema_version）；
- `EXPORT_FORMATS = ("markdown", "docx", "pdf")`；
- renderer 身份（name/version，spec M 指纹的一部分；精确 pin 保证字节确定性）。

指纹：
- `compute_export_input_fingerprint` = canonical JSON + SHA-256：export schema /
  task_id / report_id / report_fingerprint / check_result_id / check_fingerprint /
  audit_id / audit_fingerprint / human_decision_id(optional) / decision_fingerprint
  (optional) / format / renderer 身份 / normalized pack 身份。**不含** export_id /
  created_at。完全相同 → replay 同一行；report / check / audit / decision /
  pack / format / renderer 任一变化 → 新指纹 → 新 Export（旧行保留）。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

# report_exports.export_schema_version 的当前值（改名或换结构时递增；已有导出
# 原样保留，新语义 → 新指纹 → 新行）。
EXPORT_SCHEMA_VERSION = 1

# export_format 枚举（report_exports 表 CHECK 约束同步维护）。
EXPORT_FORMAT_MARKDOWN = "markdown"
EXPORT_FORMAT_DOCX = "docx"
EXPORT_FORMAT_PDF = "pdf"
EXPORT_FORMATS = (EXPORT_FORMAT_MARKDOWN, EXPORT_FORMAT_DOCX, EXPORT_FORMAT_PDF)

# renderer 身份（字节确定性；升级渲染逻辑 → 递增 version → 新指纹 → 新行）。
RENDERER_NAME_BY_FORMAT = {
    EXPORT_FORMAT_MARKDOWN: "insightforge_export_markdown",
    EXPORT_FORMAT_DOCX: "insightforge_export_docx",
    EXPORT_FORMAT_PDF: "insightforge_export_pdf",
}
RENDERER_VERSION_BY_FORMAT = {
    EXPORT_FORMAT_MARKDOWN: 1,
    EXPORT_FORMAT_DOCX: 1,
    EXPORT_FORMAT_PDF: 1,
}

# media type / file extension（与 renderer 输出一致）。
MEDIA_TYPE_BY_FORMAT = {
    EXPORT_FORMAT_MARKDOWN: "text/markdown; charset=utf-8",
    EXPORT_FORMAT_DOCX: ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    EXPORT_FORMAT_PDF: "application/pdf",
}
EXTENSION_BY_FORMAT = {
    EXPORT_FORMAT_MARKDOWN: "md",
    EXPORT_FORMAT_DOCX: "docx",
    EXPORT_FORMAT_PDF: "pdf",
}


@dataclass(frozen=True)
class ExportCitation:
    """一条导出引用（E1..En），供文档附录与段落标记使用。

    document 字段只在 origin_type=document_chunk 时有值；macro 字段只在
    origin_type=macro_observation 时有值——**不伪造 SourceRecord**（macro 的
    source_url 恒为 None）。
    """

    number: int
    evidence_card_id: UUID
    statement: str
    quote_text: str | None = None
    origin_type: str = "document_chunk"
    provider_key: str = ""
    provider_label: str = ""
    title: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime | None = None
    # document 专属
    source_url: str | None = None
    page_number: int | None = None
    xpath: str | None = None
    # macro 专属
    indicator: str | None = None
    geography: str | None = None
    period: str | None = None

    def to_identity_dict(self) -> dict:
        """normalized identity（指纹用；null 字段省略，str 确定性）。"""
        result: dict = {
            "number": self.number,
            "evidence_card_id": str(self.evidence_card_id),
            "statement": self.statement,
            "origin_type": self.origin_type,
            "provider_key": self.provider_key,
            "provider_label": self.provider_label,
        }
        for key, value in (
            ("quote_text", self.quote_text),
            ("title", self.title),
            ("published_at", self.published_at.isoformat() if self.published_at else None),
            ("fetched_at", self.fetched_at.isoformat() if self.fetched_at else None),
            ("source_url", self.source_url),
            ("page_number", self.page_number),
            ("xpath", self.xpath),
            ("indicator", self.indicator),
            ("geography", self.geography),
            ("period", self.period),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True)
class ExportParagraph:
    """一段导出正文：text 原样 + 该段引用的 citation 编号（[1][2] 标记顺序）。"""

    text: str
    citation_numbers: tuple[int, ...] = ()

    def to_identity_dict(self) -> dict:
        return {"text": self.text, "citation_numbers": list(self.citation_numbers)}


@dataclass(frozen=True)
class ExportSection:
    """一节导出正文（title + paragraphs，按 section_order 排序）。"""

    section_id: str
    title: str
    paragraphs: tuple[ExportParagraph, ...] = ()

    def to_identity_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "paragraphs": [p.to_identity_dict() for p in self.paragraphs],
        }


@dataclass(frozen=True)
class ExportReportPack:
    """一次导出的**纯结构**（renderer 的唯一输入，spec I）。

    - 正文：sections（title + paragraphs {text, citation_numbers}），text 原样，
      编号标记由 renderer 按 `citation_numbers` 追加 `[n]`，**绝不改写句子**；
    - 附录：citations E1..En（number 升序，evidence_card_id → 序号确定性）；
    - `audit_note`：人工批准路径（audit fail + human approve）时
      「本报告存在经人工确认接受的审核冲突」；audit pass 路径为 None。

    **fingerprint 身份**字段（report_fingerprint 等）不在 `to_identity_dict`
    里——它们单独进入 `compute_export_input_fingerprint`（spec M）。
    """

    export_schema_version: int
    task_id: UUID
    report_id: UUID
    analysis_as_of: date
    company_name: str
    security_code: str | None
    research_question: str
    sections: tuple[ExportSection, ...] = ()
    citations: tuple[ExportCitation, ...] = ()
    audit_note: str | None = None
    # fingerprint 身份（不参与 pack 内容指纹，单独进入 input fingerprint）
    report_fingerprint: str = ""
    check_result_id: UUID | None = None
    check_fingerprint: str = ""
    audit_id: UUID | None = None
    audit_fingerprint: str = ""
    human_decision_id: UUID | None = None
    decision_fingerprint: str | None = None

    def to_identity_dict(self) -> dict:
        """normalized pack 身份（指纹用；确定性命名字段顺序，sort_keys 兜底）。"""
        return {
            "export_schema_version": self.export_schema_version,
            "task_id": str(self.task_id),
            "report_id": str(self.report_id),
            "analysis_as_of": self.analysis_as_of.isoformat(),
            "company_name": self.company_name,
            "security_code": self.security_code,
            "research_question": self.research_question,
            "sections": [s.to_identity_dict() for s in self.sections],
            "citations": [c.to_identity_dict() for c in self.citations],
            "audit_note": self.audit_note,
        }


def compute_export_input_fingerprint(
    *,
    export_schema_version: int,
    task_id: UUID,
    report_id: UUID,
    report_fingerprint: str,
    check_result_id: UUID,
    check_fingerprint: str,
    audit_id: UUID,
    audit_fingerprint: str,
    human_decision_id: UUID | None,
    decision_fingerprint: str | None,
    format: str,
    renderer_name: str,
    renderer_version: int,
    pack_identity: dict,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8，spec M）。

    至少覆盖：export schema / task_id / report_id / report_fingerprint /
    check_result_id / check_fingerprint / audit_id / audit_fingerprint /
    human_decision_id(optional) / decision_fingerprint(optional) / format /
    renderer 身份 / normalized pack 身份。

    **不得包含** export_id / created_at。完全相同 → replay 同一行（并发 → 1 行）；
    report / check / audit / decision / pack / format / renderer 任一变化 →
    新指纹 → 新 Export（旧行保留）。
    """
    payload = {
        "export_schema_version": export_schema_version,
        "task_id": str(task_id),
        "report_id": str(report_id),
        "report_fingerprint": report_fingerprint,
        "check_result_id": str(check_result_id),
        "check_fingerprint": check_fingerprint,
        "audit_id": str(audit_id),
        "audit_fingerprint": audit_fingerprint,
        "human_decision_id": str(human_decision_id) if human_decision_id is not None else None,
        "decision_fingerprint": decision_fingerprint,
        "format": format,
        "renderer": {"name": renderer_name, "version": renderer_version},
        "pack": pack_identity,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
