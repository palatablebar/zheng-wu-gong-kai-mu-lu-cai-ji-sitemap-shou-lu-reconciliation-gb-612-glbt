"""快照 URL 合并记录：一个 canonical 一行，保留全部分片来源与裁决依据。

合并结论来自 :mod:`src.services.sitemap_merge`；``sources`` 原样保留每个分片的
loc / lastmod / source_file / 序号，``decision_basis`` 记录自动裁决规则，
``needs_review`` 的条目等人工裁决（review_status: open/resolved）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SitemapUrlRecord(Base):
    __tablename__ = "sitemap_url_records"
    __table_args__ = (
        UniqueConstraint("batch_id", "canonical_url", name="uq_sitemap_url_batch_canonical"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_batches.id", ondelete="CASCADE"), index=True
    )
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    loc: Mapped[str] = mapped_column(String(300))
    canonical_url: Mapped[str] = mapped_column(String(300), index=True)
    lastmod: Mapped[str | None] = mapped_column(String(30), nullable=True)
    changefreq: Mapped[str | None] = mapped_column(String(20), nullable=True)
    priority: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_file: Mapped[str] = mapped_column(String(120), default="sitemap.xml")

    decision_basis: Mapped[str] = mapped_column(String(30), default="single_source")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_status: Mapped[str] = mapped_column(String(20), default="none", index=True)
    review_reasons: Mapped[list] = mapped_column(JSON, default=list)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    lastmod_values: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_lastmod: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
