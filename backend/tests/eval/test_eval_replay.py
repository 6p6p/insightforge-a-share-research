"""EvaluationReplayRehydrator tamper / isolation tests（stage 7B.1.4B.1, spec P）。

不依赖真实 PostgreSQL：所有 failure path 在 `rehydrate_case` 的 stage one
（字节 SHA / snapshot fingerprint / 跨引用 / media_type dispatch）就 fail-fast，
**不会打开 target session**。target sessionmaker 指向一个永不连接的 bogus URL，
若任一 tamper 未能触发早期失败，测试会以错误异常类型失败（loud failure）。

覆盖：
1. blob tamper（content_sha256 mismatch）；
2. snapshot fingerprint tamper（bundle 不自洽）；
3. 跨引用破坏（document provider_key 不在 source_providers）；
4. unsupported media_type；
5. 结构隔离（rehydrator 只持有 target_sessionmaker + raw_store + loader）；
6. 错误消息不泄露 sensitive payload（DB URL / raw bytes）。
"""

import hashlib
import json
from pathlib import Path

import pytest

from app.db.session import DatabaseManager
from app.eval.bundle.layout import document_blob_path, snapshot_path
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.errors import EvalReplayError, EvalReplayIntegrityError
from app.eval.replay import EvaluationReplayRehydrator
from app.services.macro_persistence_service import MacroPersistenceService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.replay_bundle import CASE_ID, CASE_VERSION, build_replay_bundle

_BOGUS_URL = "postgresql+psycopg://user:pass@127.0.0.1:1/nope"


def _rehydrator(bundle_root: Path, raw_root: Path) -> EvaluationReplayRehydrator:
    """构造一个永不真正连接 target DB 的 rehydrator（failure path 不触 DB）。"""
    manager = DatabaseManager(database_url=_BOGUS_URL, echo=False, connect_timeout_seconds=1)
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    return EvaluationReplayRehydrator(
        manager.session_factory(),
        store,
        EvaluationBundleLoader(bundle_root),
    )


@pytest.mark.asyncio
async def test_blob_tamper_rejected(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    spec = build_replay_bundle(bundle_root)
    # 篡改归档字节，但 content_sha256 不变 → rehydrator 必须拒绝。
    document_blob_path(bundle_root, spec.document_sha256).write_bytes(b"tampered bytes")
    rehydrator = _rehydrator(bundle_root, tmp_path / "raw")

    with pytest.raises(EvalReplayIntegrityError) as exc:
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
    assert "content_sha256" in str(exc.value)


@pytest.mark.asyncio
async def test_snapshot_fingerprint_tamper_rejected(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    spec = build_replay_bundle(bundle_root)
    # 改 snapshot 语义字段但不更新 case 引用 → snapshot fingerprint 与 case 不一致。
    snap = snapshot_path(bundle_root, spec.snapshot_fingerprint)
    data = json.loads(snap.read_text(encoding="utf-8"))
    data["document_sources"][0]["title"] = "tampered title"
    snap.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rehydrator = _rehydrator(bundle_root, tmp_path / "raw")

    with pytest.raises(EvalReplayIntegrityError) as exc:
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
    assert "fingerprint" in str(exc.value)


@pytest.mark.asyncio
async def test_document_provider_not_in_providers_rejected(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root, doc_provider_key="ghost")
    rehydrator = _rehydrator(bundle_root, tmp_path / "raw")

    with pytest.raises(EvalReplayIntegrityError) as exc:
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
    assert "provider_key" in str(exc.value)


@pytest.mark.asyncio
async def test_unsupported_media_type_rejected(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root, doc_media_type="application/x-unknown")
    rehydrator = _rehydrator(bundle_root, tmp_path / "raw")

    with pytest.raises(EvalReplayError) as exc:
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
    assert "media_type" in str(exc.value)


def test_rehydrator_structurally_isolated(tmp_path) -> None:
    """结构隔离：rehydrator 只持有注入依赖 + 派生 helper，无 source/live sessionmaker。

    `_macro_service` 是 `MacroPersistenceService(target_sessionmaker, raw_store)` 的
    派生 wrapper（用于 macro closure 的 fingerprint 一致性校验），**只包装**同一对
    注入依赖，不引入任何 source/live 引用。
    """
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)
    manager = DatabaseManager(database_url=_BOGUS_URL, echo=False, connect_timeout_seconds=1)
    store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    loader = EvaluationBundleLoader(bundle_root)

    rehydrator = EvaluationReplayRehydrator(manager.session_factory(), store, loader)

    assert set(vars(rehydrator)) == {
        "_sessionmaker",
        "_raw_store",
        "_loader",
        "_macro_service",
    }
    assert rehydrator._loader is loader
    assert isinstance(rehydrator._macro_service, MacroPersistenceService)


@pytest.mark.asyncio
async def test_error_message_excludes_sensitive_payload(tmp_path) -> None:
    bundle_root = tmp_path / "bundle"
    spec = build_replay_bundle(bundle_root)
    document_blob_path(bundle_root, spec.document_sha256).write_bytes(b"tampered bytes")
    rehydrator = _rehydrator(bundle_root, tmp_path / "raw")

    with pytest.raises(EvalReplayIntegrityError) as exc:
        await rehydrator.rehydrate_case(CASE_ID, CASE_VERSION)
    message = str(exc.value)
    # 不泄露 DB URL / raw bytes / payload 内容。
    assert "postgresql" not in message
    assert "127.0.0.1" not in message
    assert "tampered bytes" not in message
    assert hashlib.sha256(b"tampered bytes").hexdigest() not in message
