"""Research fulfillment production factory wiring test (stage 7A.2A spec D/G).

纯构造冒烟（非集成，**0 网络 / 0 LLM / 0 Chroma / 0 DB 连接**）：验证
`create_research_fulfillment_service` 按 Settings 装配完整生产服务。所有模型
adapter 惰性加载（langchain DeepSeek / SentenceTransformer 只在首次调用时
import/load），构造阶段不触发任何外部调用。

断言（对照 task D 最小 wiring 目标）：
- 返回完整 `ResearchFulfillmentService`；
- document executor 注入**真实** `SourceIndexBuilder`（archived+parsed →
  Chunking → VectorIndex，生产 no-index 自动补建路径）；
- retrieval 与 index 共用**同一 production 默认 collection**
  （BGE spec 派生 `insightforge_chunks_v2_<fp12>`，所有公司 / ChunkSet 共享）。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.research_fulfillment.executors import DocumentNeedExecutor, SourceIndexBuilder
from app.research_fulfillment.factory import create_research_fulfillment_service
from app.research_fulfillment.service import ResearchFulfillmentService


def _settings() -> Settings:
    """显式构造 Settings（`_env_file=None`，不读真实环境 / .env）。

    默认 `llm_provider=deepseek`（config 默认），factory 分派到真实
    DeepSeek adapter；但构造完全惰性，不触发任何 API / 网络 / key 读取。
    """
    return Settings(
        _env_file=None,
        app_env="test",
        log_level="DEBUG",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )


def test_create_research_fulfillment_service_assembles_production_wiring() -> None:
    service = create_research_fulfillment_service(_settings(), async_sessionmaker())

    assert isinstance(service, ResearchFulfillmentService)

    document = service._executors["document"]
    assert isinstance(document, DocumentNeedExecutor)
    # document 路径注入真实 SourceIndexBuilder（生产 no-index 自动补建）。
    assert isinstance(document._index_builder, SourceIndexBuilder)
    # retrieval 与 index 共用同一 production 默认 collection（BGE spec 派生）。
    assert (
        document._retrieval._collection_name == document._index_builder._indexing._collection_name
    )
    assert document._retrieval._collection_name.startswith("insightforge_chunks_v2_")
