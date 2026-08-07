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
    """Candidate 验证状态。当前只有 unverified。

    2D.1 只产生"线索"：Candidate 未经验证，原始发布网页是否可访问、
    是否与公司相关均未知。verified / rejected / archived / evidence_ready
    属于 2D.2 的 Original Source Verification，本阶段不提前引入。
    """

    UNVERIFIED = "unverified"
