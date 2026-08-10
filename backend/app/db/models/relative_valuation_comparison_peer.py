"""SQLAlchemy model for relative valuation comparison ↔ peer links (stage 4C.2A).

`relative_valuation_comparison_peers` 记录一次比较绑定了哪些 peer company /
peer observation：

- PK(comparison_id, peer_company_id)——同一 comparison 对同一 peer 公司只出现
  一次（peer 集合按公司去重）；
- UNIQUE(comparison_id, peer_observation_id)——同一 observation 不能被同一
  comparison 重复绑定；
- comparison_id FK comparisons **CASCADE**（删 comparison 删 link）；
  peer_company_id FK companies **RESTRICT**；peer_observation_id FK
  valuation_metric_observations **RESTRICT**（上游存在期间 link 不静默消失）。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RelativeValuationComparisonPeerModel(Base):
    __tablename__ = "relative_valuation_comparison_peers"
    __table_args__ = (
        PrimaryKeyConstraint(
            "comparison_id",
            "peer_company_id",
            name="pk_relative_valuation_comparison_peers",
        ),
        UniqueConstraint(
            "comparison_id",
            "peer_observation_id",
            name="uq_relative_valuation_comparison_peers_observation",
        ),
        Index(
            "ix_relative_valuation_comparison_peers_peer_observation_id",
            "peer_observation_id",
        ),
    )

    comparison_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("relative_valuation_comparisons.comparison_id", ondelete="CASCADE"),
        nullable=False,
    )
    peer_company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    peer_observation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "valuation_metric_observations.valuation_observation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
