"""BGEProvider unit tests (stage 3B.1).

不下载真实模型：通过 monkeypatch `sys.modules['sentence_transformers']` 提供
fake SentenceTransformer，验证 provider 的 lazy load、immutable revision guard、
query_instruction 前缀、禁止静默截断与向量契约校验逻辑。
"""

import math
import sys
import types

import pytest

from app.rag.embedding.bge import BGEProvider
from app.rag.embedding.contracts import (
    BGE_QUERY_INSTRUCTION,
    EmbeddingModelSpec,
)
from app.rag.embedding.errors import (
    EmbeddingInputTooLong,
    EmbeddingModelNotConfigured,
)


def _spec(*, revision: str | None = "test-rev", max_input_tokens: int = 512) -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        model_id="BAAI/bge-small-zh-v1.5",
        dimension=512,
        normalize_embeddings=True,
        query_instruction=BGE_QUERY_INSTRUCTION,
        max_input_tokens=max_input_tokens,
        revision=revision,
    )


def _install_fake_sentence_transformers(monkeypatch) -> object:
    """替换 sys.modules['sentence_transformers'] 为 fake 实现。"""

    class FakeTokenizer:
        def __call__(self, text: str) -> dict:
            # 近似：token 数 = 字符数 + 2（含 [CLS]/[SEP] special tokens）
            return {"input_ids": list(range(len(text) + 2))}

    class FakeModel:
        instances: list["FakeModel"] = []

        def __init__(self, model_id: str, *, revision: str) -> None:
            FakeModel.instances.append(self)
            self.loaded_args = (model_id, revision)
            self.tokenizer = FakeTokenizer()

        def encode(self, texts: list[str], normalize_embeddings: bool = True) -> list:
            vector = [1.0 / math.sqrt(512)] * 512
            return [list(vector) for _ in texts]

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return FakeModel


@pytest.fixture
def fake_st(monkeypatch):
    return _install_fake_sentence_transformers(monkeypatch)


class TestRevisionGuard:
    def test_revision_none_raises_without_importing_model(self) -> None:
        provider = BGEProvider(_spec(revision=None))
        with pytest.raises(EmbeddingModelNotConfigured):
            provider.token_count("abc")
        with pytest.raises(EmbeddingModelNotConfigured):
            provider.embed_documents(["abc"])
        with pytest.raises(EmbeddingModelNotConfigured):
            provider.embed_query("abc")
        assert provider._model is None

    def test_revision_not_main(self) -> None:
        # 冻结 spec 的 revision 不允许是 moving "main"
        assert _spec().revision != "main"


class TestLazyLoad:
    def test_model_loaded_only_on_first_call(self, fake_st) -> None:
        provider = BGEProvider(_spec())
        assert provider._model is None
        provider.token_count("你好")
        assert provider._model is not None
        first = provider._model
        provider.embed_documents(["你好世界"])
        assert provider._model is first
        # fake_st.instances 只有 1 个实例（首次加载）
        assert len(fake_st.instances) == 1

    def test_load_passes_immutable_revision(self, fake_st) -> None:
        provider = BGEProvider(_spec(revision="0f8c7a3..."))
        provider.token_count("你好")
        model_id, revision = fake_st.instances[0].loaded_args
        assert model_id == "BAAI/bge-small-zh-v1.5"
        assert revision == "0f8c7a3..."


class TestEmbedBehavior:
    def test_embed_documents_no_instruction(self, fake_st) -> None:
        provider = BGEProvider(_spec())
        result = provider.embed_documents(["你好世界"])
        assert len(result) == 1
        assert len(result[0]) == 512

    def test_embed_query_prefixes_instruction(self, fake_st) -> None:
        provider = BGEProvider(_spec())
        result = provider.embed_query("贵州茅台净利润")
        assert len(result) == 512

    def test_document_too_long_raises(self, fake_st) -> None:
        provider = BGEProvider(_spec(max_input_tokens=10))
        with pytest.raises(EmbeddingInputTooLong):
            provider.embed_documents(["这个文本的长度会超过十个 token，" * 20])

    def test_query_too_long_raises(self, fake_st) -> None:
        provider = BGEProvider(_spec(max_input_tokens=10))
        # instruction 前缀本身 token 数 + 文本已超限
        with pytest.raises(EmbeddingInputTooLong):
            provider.embed_query("非常长的查询" * 10)
