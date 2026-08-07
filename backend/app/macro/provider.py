"""Macro data provider protocol (stage 2C.1).

不创建 BaseProvider 抽象类；只定义 Protocol，实现方负责与具体 API 交互。
Provider.fetch 不得写数据库、不得调用 SourceIngestionService。
"""

from typing import Protocol

from app.macro.contracts import MacroFetchResult, MacroQuery


class MacroDataProvider(Protocol):
    """宏观数据 Provider 契约。

    - provider_key：与 Source Registry 中登记的 provider_key 一致；
    - fetch：一次完整获取（指标/国家元数据 + 观测值），返回 MacroFetchResult；
    - 只做获取与解析，不持久化、不生成结论。
    """

    provider_key: str

    async def fetch(
        self,
        query: MacroQuery,
    ) -> MacroFetchResult: ...
