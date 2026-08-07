"""Macro persistence service (stage 2C.2B).

persist_captured_fetch 严格遵循写入顺序 A-K：

A. 校验 CapturedMacroFetch 完整性（capture_validation，抛 MacroCaptureInvalid）；
B. 每个原始响应先写入内容寻址文件（文件 I/O 在 DB transaction 之前；
   orphan 文件保留不删，等待后续 GC）；
C. 依据归档后的 artifact 内容 SHA-256 计算 snapshot fingerprint；
D. 开启短 DB transaction（网络 I/O 期间绝不持有 Session——本 service 无网络）；
E. series get_or_create（ON CONFLICT DO UPDATE no-op，并发下只保留一行）；
F. raw artifact rows get_or_create（ON CONFLICT DO NOTHING + 回查）；
G. fingerprint 去重：create_or_get_by_fingerprint（ON CONFLICT DO NOTHING，
   只有赢得 insert 的事务 created=True）；
H. replay 完整性检查：已存在 Snapshot 必须与本次获取一致
   （series / fingerprint 版本 / link 数 / 观测数），不一致抛
   MacroSnapshotIntegrityError，不自动修复；
I. 仅赢家新建 Snapshot；
J. 仅赢家写 Artifact Links；
K. 仅赢家写 Observations；
L. flush + commit；任何 DB 层异常回滚并抛 MacroPersistenceFailed。

并发语义（§十一）：并发相同 fingerprint 只允许一个事务创建 Snapshot 及其
Links/Observations；输家回查既有 Snapshot 并做 replay 完整性检查后返回
replayed=True。series 的 DO UPDATE 会等待冲突事务提交，从而在"新 series
首次并发"场景天然串行化，避免 DO NOTHING + 回查看到未提交行的竞态。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.raw_artifact import RawArtifactModel
from app.domain.macro_persistence import MacroSnapshotStatus
from app.macro.capture import CapturedMacroFetch
from app.macro.capture_validation import validate_captured_macro_fetch
from app.macro.contracts import (
    MacroFrequency,
    MacroGeographyType,
    MacroPeriodSemantics,
    MacroQuery,
)
from app.macro.errors import (
    MacroArtifactConflict,
    MacroPersistenceFailed,
    MacroSnapshotIntegrityError,
)
from app.macro.fingerprint import (
    MACRO_SNAPSHOT_FINGERPRINT_VERSION,
    WORLD_BANK_NORMALIZATION_VERSION,
    FingerprintArtifact,
    build_macro_snapshot_fingerprint,
)
from app.macro.world_bank.provider import WorldBankProvider
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_series_repository import MacroSeriesRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.storage.raw_store import LocalRawArtifactStore, StoredRawArtifact

_MEDIA_TYPE_JSON = "application/json"


@dataclass(frozen=True)
class MacroPersistenceResult:
    """一次 persist_captured_fetch 的结果摘要（不含任何原始响应正文）。"""

    series_id: UUID
    snapshot_id: UUID
    snapshot_fingerprint: str
    replayed: bool
    artifact_count: int
    observation_count: int


class MacroPersistenceService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store

    async def fetch_and_persist(
        self,
        provider: WorldBankProvider,
        query: MacroQuery,
    ) -> MacroPersistenceResult:
        """获取 + 持久化一步完成。

        网络 I/O（provider.fetch_with_capture）在 persist_captured_fetch 开启
        DB transaction 之前完成，因此网络 I/O 期间绝不持有 AsyncSession。
        """
        captured = await provider.fetch_with_capture(query)
        return await self.persist_captured_fetch(captured)

    async def persist_captured_fetch(
        self,
        captured: CapturedMacroFetch,
    ) -> MacroPersistenceResult:
        # A. 完整性校验（缺页/重复/hostname/content-type 等）
        validate_captured_macro_fetch(captured)
        result = captured.result

        # B. 原始响应先写内容寻址文件；文件 I/O 在 DB transaction 之前。
        #    相同内容复用（newly_created=False）；孤儿文件保留不删。
        stored_artifacts = [
            self._raw_store.put_json_bytes(response.raw_bytes)
            for response in captured.responses
        ]

        # C. 计算 snapshot fingerprint（基于归档 artifact 的内容 SHA-256）。
        fingerprint_artifacts = tuple(
            FingerprintArtifact(
                role=response.role,
                page=response.page,
                sha256=stored.content_sha256,
                response_status=response.response_status,
                final_hostname=response.final_hostname,
                content_type=_MEDIA_TYPE_JSON,
            )
            for response, stored in zip(captured.responses, stored_artifacts, strict=True)
        )
        fingerprint = build_macro_snapshot_fingerprint(result, fingerprint_artifacts)

        # D-L. 短 DB transaction（无网络 I/O，可持有 Session）。
        async with self._sessionmaker() as session:
            try:
                series_repo = MacroSeriesRepository(session)
                artifact_repo = RawArtifactRepository(session)
                snapshot_repo = MacroSnapshotRepository(session)
                observation_repo = MacroObservationRepository(session)

                # E. series get_or_create：稳定身份六字段。
                series, _ = await series_repo.get_or_create(
                    MacroSeriesModel(
                        series_id=uuid.uuid4(),
                        provider_key=result.provider_key,
                        source_id=result.source_id,
                        external_indicator_id=result.indicator.external_indicator_id,
                        geography_type=MacroGeographyType.COUNTRY.value,
                        geography_code=result.geography.iso3_code,
                        frequency=MacroFrequency.ANNUAL.value,
                    )
                )

                # F. raw artifact rows get_or_create；内容寻址冲突即复用既有行。
                artifact_rows = await self._load_artifact_rows(artifact_repo, stored_artifacts)

                # G. fingerprint 去重：并发相同 fingerprint 只有一个 created=True。
                snapshot = self._build_snapshot_model(series.series_id, fingerprint, captured)
                snapshot, created = await snapshot_repo.create_or_get_by_fingerprint(snapshot)
                # 立即捕获标识：commit 后 returning 行/实体可能被 expire，避免
                # async 下访问过期属性触发 MissingGreenlet。
                series_id = series.series_id
                snapshot_id = snapshot.snapshot_id
                snapshot_fingerprint = snapshot.snapshot_fingerprint

                # H. replay 完整性检查（不自动修复）。
                if not created:
                    await self._verify_replay(
                        snapshot_repo,
                        observation_repo,
                        snapshot,
                        series_id,
                        snapshot_fingerprint,
                        len(captured.responses),
                        len(result.observations),
                    )
                    return MacroPersistenceResult(
                        series_id=series_id,
                        snapshot_id=snapshot_id,
                        snapshot_fingerprint=snapshot_fingerprint,
                        replayed=True,
                        artifact_count=await snapshot_repo.count_artifact_links(snapshot_id),
                        observation_count=await observation_repo.count_for_snapshot(snapshot_id),
                    )

                # I. 仅赢家：Snapshot 已由 create_or_get_by_fingerprint 插入。

                # J. 仅赢家写 Artifact Links。
                for response, artifact in zip(captured.responses, artifact_rows, strict=True):
                    await snapshot_repo.add_artifact_link(
                        MacroSnapshotArtifactModel(
                            snapshot_id=snapshot_id,
                            artifact_id=artifact.artifact_id,
                            role=response.role.value,
                            page=response.page,
                            response_status=response.response_status,
                            final_hostname=response.final_hostname,
                            content_type=response.content_type,
                            fetched_at=response.fetched_at,
                        )
                    )

                # K. 仅赢家写 Observations（value_numeric 直接使用 Decimal，禁止 float）。
                observation_count = 0
                if result.observations:
                    observations = [
                        MacroObservationModel(
                            snapshot_id=snapshot_id,
                            period=observation.period,
                            normalized_period_start=observation.normalized_period_start,
                            period_semantics=MacroPeriodSemantics.PROVIDER_YEAR_LABEL.value,
                            frequency=MacroFrequency.ANNUAL.value,
                            value_numeric=observation.value,
                            is_missing=observation.is_missing,
                            observation_status=observation.observation_status,
                            decimal_scale=observation.decimal_scale,
                        )
                        for observation in result.observations
                    ]
                    observation_count = await observation_repo.bulk_create(observations)

                # L. flush + commit。
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise MacroPersistenceFailed() from exc

            return MacroPersistenceResult(
                series_id=series_id,
                snapshot_id=snapshot_id,
                snapshot_fingerprint=snapshot_fingerprint,
                replayed=False,
                artifact_count=len(captured.responses),
                observation_count=observation_count,
            )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    async def _load_artifact_rows(
        artifact_repo: RawArtifactRepository,
        stored_artifacts: list[StoredRawArtifact],
    ) -> list[RawArtifactModel]:
        """raw artifact rows get_or_create；既有行元数据与本次不一致视为冲突。"""
        rows: list[RawArtifactModel] = []
        for stored in stored_artifacts:
            artifact, _ = await artifact_repo.get_or_create(
                RawArtifactModel(
                    content_sha256=stored.content_sha256,
                    storage_key=stored.storage_key,
                    byte_size=stored.byte_size,
                    media_type=_MEDIA_TYPE_JSON,
                )
            )
            if (
                artifact.storage_key != stored.storage_key
                or artifact.media_type != _MEDIA_TYPE_JSON
                or artifact.byte_size != stored.byte_size
            ):
                raise MacroArtifactConflict("existing raw artifact metadata mismatch")
            rows.append(artifact)
        return rows

    @staticmethod
    def _build_snapshot_model(
        series_id: UUID,
        fingerprint: str,
        captured: CapturedMacroFetch,
    ) -> MacroDatasetSnapshotModel:
        result = captured.result
        return MacroDatasetSnapshotModel(
            # 显式主键：create_or_get_by_fingerprint 用 Core insert 全列赋值，
            # Python 侧 uuid4 default 对显式传入 None 的列不会触发。
            snapshot_id=uuid.uuid4(),
            series_id=series_id,
            snapshot_fingerprint=fingerprint,
            fingerprint_version=MACRO_SNAPSHOT_FINGERPRINT_VERSION,
            normalization_version=WORLD_BANK_NORMALIZATION_VERSION,
            requested_country_code=result.query.country_code,
            query_start_year=result.query.start_year,
            query_end_year=result.query.end_year,
            source_id_snapshot=result.source_id,
            indicator_name=result.indicator.name,
            indicator_unit=result.indicator.unit,
            source_name=result.indicator.source_name,
            source_note=result.indicator.source_note,
            source_organization=result.indicator.source_organization,
            topics_snapshot=sorted(
                (
                    {"topic_id": topic.topic_id, "name": topic.name}
                    for topic in result.indicator.topics
                ),
                key=lambda topic: (topic["topic_id"], topic["name"]),
            ),
            provider_country_id=result.geography.provider_country_id,
            iso2_code=result.geography.iso2_code,
            iso3_code=result.geography.iso3_code,
            geography_name=result.geography.name,
            region_name=result.geography.region_name,
            income_level_name=result.geography.income_level_name,
            page=result.page_info.page,
            pages=result.page_info.pages,
            per_page=result.page_info.per_page,
            provider_total=result.page_info.total,
            provider_last_updated=result.page_info.last_updated,
            fetched_at=result.fetched_at,
            request_count=result.request_count,
            acquisition_method=result.acquisition_method.value,
            authority_tier_snapshot=int(result.authority_tier),
            critical_claim_eligible_snapshot=result.critical_claim_eligible,
            provider_capabilities_snapshot=[cap.value for cap in result.provider_capabilities],
            status=MacroSnapshotStatus.AVAILABLE.value,
        )

    @staticmethod
    async def _verify_replay(
        snapshot_repo: MacroSnapshotRepository,
        observation_repo: MacroObservationRepository,
        snapshot: MacroDatasetSnapshotModel,
        series_id: UUID,
        fingerprint: str,
        expected_links: int,
        expected_observations: int,
    ) -> None:
        """已存在 Snapshot 的 replay 完整性检查；失败抛 MacroSnapshotIntegrityError。"""
        if snapshot.series_id != series_id:
            raise MacroSnapshotIntegrityError("replay series mismatch")
        if snapshot.snapshot_fingerprint != fingerprint:
            raise MacroSnapshotIntegrityError("replay fingerprint mismatch")
        if snapshot.fingerprint_version != MACRO_SNAPSHOT_FINGERPRINT_VERSION:
            raise MacroSnapshotIntegrityError("replay fingerprint version mismatch")
        if snapshot.normalization_version != WORLD_BANK_NORMALIZATION_VERSION:
            raise MacroSnapshotIntegrityError("replay normalization version mismatch")
        links = await snapshot_repo.count_artifact_links(snapshot.snapshot_id)
        if links != expected_links:
            raise MacroSnapshotIntegrityError("replay artifact link count mismatch")
        observations = await observation_repo.count_for_snapshot(snapshot.snapshot_id)
        if observations != expected_observations:
            raise MacroSnapshotIntegrityError("replay observation count mismatch")
