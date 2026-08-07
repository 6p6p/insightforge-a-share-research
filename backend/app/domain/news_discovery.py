"""News discovery domain enums (stage 2D.1).

发现（Discovery）与事实来源（Source）分离：NewsDiscoveryEngine 标识"发现机制"
（当前只有 GDELT DOC 2.0），不是 SourceProvider。AcquisitionMethod 复用
sources.py 中已有的 WEB_SEARCH_DISCOVERY，不新增同义枚举。
"""

from enum import StrEnum


class NewsDiscoveryEngine(StrEnum):
    """新闻发现引擎。当前只有 GDELT DOC 2.0（discovery-only）。"""

    GDELT_DOC = "gdelt_doc"


class NewsDiscoveryStatus(StrEnum):
    """Discovery Run 状态。当前只有 available。"""

    AVAILABLE = "available"


class NewsCandidateVerificationStatus(StrEnum):
    """Candidate 验证状态（冻结为 unverified + verified，二者唯一）。

    2D.1 只产生"线索"：Candidate 未经验证。2D.2A 定义 verified 的严格语义：
    仅表示"原始发布网页属于 Source Registry 登记的原创媒体、公开页面被安全
    抓取、raw HTML 已不可变归档、Candidate → SourceRecord 溯源已建立"；
    不代表新闻内容为真、不代表已交叉验证、不代表支持关键声明、更不是 Evidence。
    失败不改状态：发布者不支持 / 抓取失败 / 内容被拒 → Candidate 保持
    unverified，不存在终态 rejected。未来的失败历史由独立 Attempt 模型记录
    （如 NewsSourceVerificationAttempt），不会把 verification_status 演进为
    rejected / archived / evidence_ready（见 ADR-0015）。
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
