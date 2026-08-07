"""Macro persistence domain enums (stage 2C.2A).

只冻结持久化数据模型需要的枚举；不引入 fetching/failed/parsing/evidence_ready
等 2C.2A 尚未实现的语义。
"""

from enum import StrEnum


class MacroSnapshotArtifactRole(StrEnum):
    """MacroSnapshot 一次获取中某个原始响应扮演的角色。"""

    INDICATOR_METADATA = "indicator_metadata"
    COUNTRY_METADATA = "country_metadata"
    OBSERVATIONS_PAGE = "observations_page"


class MacroSnapshotStatus(StrEnum):
    """MacroDatasetSnapshot 的状态；当前唯一合法值 available。"""

    AVAILABLE = "available"
