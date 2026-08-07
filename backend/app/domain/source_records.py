"""Source records domain enums."""

from enum import StrEnum


class SourceDocumentType(StrEnum):
    ANNUAL_REPORT = "annual_report"
    SEMIANNUAL_REPORT = "semiannual_report"
    QUARTERLY_REPORT = "quarterly_report"
    COMPANY_ANNOUNCEMENT = "company_announcement"
    ISSUER_IR_MATERIAL = "issuer_ir_material"
    PROSPECTUS = "prospectus"
    OTHER = "other"


class SourceRecordStatus(StrEnum):
    AVAILABLE = "available"


class RawArtifactMediaType(StrEnum):
    PDF = "application/pdf"
    JSON = "application/json"
