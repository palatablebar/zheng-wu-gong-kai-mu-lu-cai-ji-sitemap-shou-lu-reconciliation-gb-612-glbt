"""sitemap.xml 里的一个 <url> 条目（三向对账的 B 集合）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SitemapEntry(Base):
    __tablename__ = "sitemap_entries"
    __table_args__ = (UniqueConstraint("site_id", "loc", name="uq_sitemap_site_loc"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    # 该主表条目来自哪个已激活快照批次；半批（expanding/ready/failed）永远不会写进来
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("sitemap_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    loc: Mapped[str] = mapped_column(String(300))
    canonical_url: Mapped[str] = mapped_column(String(300), index=True)
    lastmod: Mapped[str | None] = mapped_column(String(20), nullable=True)
    changefreq: Mapped[str | None] = mapped_column(String(20), nullable=True)
    priority: Mapped[float] = mapped_column(Float, default=0.8)
    source_file: Mapped[str] = mapped_column(String(120), default="sitemap.xml")
    # 合并裁决依据与待复核状态（从快照记录带过来，页面/导出可直接展示）
    decision_basis: Mapped[str] = mapped_column(String(30), default="single_source")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_status: Mapped[str] = mapped_column(String(20), default="none")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
