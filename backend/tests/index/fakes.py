"""In-memory fake Chroma client + manager for vector index tests (stage 3B.1).

模拟 chromadb-client 1.5.9 的 async 语义：
- get_or_create_collection 同名时**静默返回既有 collection**（不校验 metadata），
  因此"配置不一致 → VectorCollectionConflict"必须由服务读回 metadata 自行判定；
- upsert 按 id 覆盖（幂等），记录 id → (embedding, metadata)；
- get 支持 ids 精确过滤与 where 过滤；query 支持 cosine 距离 + where 过滤；
- delete_collection 删除整 collection（供测试清理）。

不含任何网络；供服务端到端测试与契约测试使用。
"""

from dataclasses import dataclass


@dataclass
class _Record:
    id: str
    embedding: list[float]
    metadata: dict


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot  # 约定已归一化（BGE normalize=true），cos = dot


def _matches_where(metadata: dict, where: dict | None) -> bool:
    if where is None:
        return True
    if "$and" in where:
        return all(_matches_where(metadata, cond) for cond in where["$and"])
    if "$or" in where:
        return any(_matches_where(metadata, cond) for cond in where["$or"])
    for key, cond in where.items():
        if not isinstance(cond, dict):
            if metadata.get(key) != cond:
                return False
        else:
            value = metadata.get(key)
            if "$eq" in cond and value != cond["$eq"]:
                return False
            if "$ne" in cond and value == cond["$ne"]:
                return False
            if "$in" in cond and value not in cond["$in"]:
                return False
            if "$nin" in cond and value in cond["$nin"]:
                return False
            if "$gt" in cond and not (isinstance(value, (int, float)) and value > cond["$gt"]):
                return False
            if "$gte" in cond and not (isinstance(value, (int, float)) and value >= cond["$gte"]):
                return False
            if "$lt" in cond and not (isinstance(value, (int, float)) and value < cond["$lt"]):
                return False
            if "$lte" in cond and not (isinstance(value, (int, float)) and value <= cond["$lte"]):
                return False
    return True


class FakeCollection:
    """In-memory Chroma collection (upsert by id; get/query with where filter)."""

    def __init__(self, name: str, metadata: dict | None) -> None:
        self.name = name
        self.metadata = metadata or {}
        self._records: dict[str, _Record] = {}

    async def upsert(self, *, ids, embeddings=None, metadatas=None, **_) -> None:
        for record_id, embedding, metadata in zip(ids, embeddings, metadatas, strict=True):
            self._records[str(record_id)] = _Record(
                id=str(record_id), embedding=list(embedding), metadata=dict(metadata)
            )

    async def count(self, **_) -> int:
        return len(self._records)

    async def get(self, *, ids=None, where=None, include=None, **_) -> dict:
        include = include or ["metadatas", "documents"]
        selected = list(self._records.values())
        if ids is not None:
            id_set = {str(i) for i in ids}
            selected = [r for r in selected if r.id in id_set]
        if where is not None:
            selected = [r for r in selected if _matches_where(r.metadata, where)]
        result: dict = {"ids": [r.id for r in selected]}
        if "metadatas" in include:
            result["metadatas"] = [r.metadata for r in selected]
        if "embeddings" in include:
            result["embeddings"] = [r.embedding for r in selected]
        if "documents" in include:
            result["documents"] = [None] * len(selected)
        return result

    async def query(
        self, *, query_embeddings=None, n_results=10, where=None, include=None, **_
    ) -> dict:
        query = query_embeddings[0] if query_embeddings else []
        scored = []
        for record in self._records.values():
            if where is not None and not _matches_where(record.metadata, where):
                continue
            scored.append((record, _cosine_similarity(query, record.embedding)))
        scored.sort(key=lambda pair: pair[1], reverse=True)  # 距离升序 = 相似度降序
        scored = scored[:n_results]
        include = include or ["metadatas", "documents", "distances"]
        result: dict = {"ids": [[r.id for r, _ in scored]]}
        if "distances" in include:
            result["distances"] = [[1.0 - sim for _, sim in scored]]
        if "metadatas" in include:
            result["metadatas"] = [[r.metadata for r, _ in scored]]
        if "documents" in include:
            result["documents"] = [[None] * len(scored)]
        return result

    async def delete(self, *, ids=None, **_) -> None:
        if ids is None:
            self._records.clear()
        else:
            for record_id in ids:
                self._records.pop(str(record_id), None)


class FakeChromaClient:
    """In-memory Chroma client：同名 collection 静默返回既有（模拟真实语义）。"""

    def __init__(self) -> None:
        self._collections: dict[str, FakeCollection] = {}

    async def get_or_create_collection(
        self, name, configuration=None, metadata=None, **_
    ) -> FakeCollection:
        if name in self._collections:
            return self._collections[name]
        collection = FakeCollection(name, metadata)
        self._collections[name] = collection
        return collection

    async def list_collections(self, **_):
        return list(self._collections.values())

    async def delete_collection(self, name, **_) -> None:
        self._collections.pop(name, None)


class FakeChromaManager:
    """替身 ChromaManager：get_client() 返回内存 FakeChromaClient。"""

    def __init__(self) -> None:
        self._client = FakeChromaClient()

    async def get_client(self) -> FakeChromaClient:
        return self._client

    async def heartbeat(self) -> None:
        return None

    @property
    def client(self) -> FakeChromaClient:
        return self._client
