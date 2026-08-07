"""Macro data domain (stage 2C.1).

本阶段只建立契约与 World Bank Provider，不写数据库、不持久化；
MacroFetchResult 不是 Evidence。
"""

from app.macro.contracts import (
    MacroFetchResult,
    MacroFrequency,
    MacroGeography,
    MacroGeographyType,
    MacroIndicator,
    MacroObservation,
    MacroPageInfo,
    MacroQuery,
    MacroTopic,
)
from app.macro.provider import MacroDataProvider

__all__ = [
    "MacroDataProvider",
    "MacroFetchResult",
    "MacroFrequency",
    "MacroGeography",
    "MacroGeographyType",
    "MacroIndicator",
    "MacroObservation",
    "MacroPageInfo",
    "MacroQuery",
    "MacroTopic",
]
