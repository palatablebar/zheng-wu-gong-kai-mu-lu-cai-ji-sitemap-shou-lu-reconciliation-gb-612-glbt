"""快照分片检查点：索引展开的断点续跑依据（一个叶子分片一行）。

``status``：``pending`` 待读 / ``done`` 已落记录 / ``failed`` 读取失败（可重试）。
续跑只挑非 done 的分片，已 done 的分片永不重读、不重复写记录。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SitemapShard(Base):
    __tablename__ = "sitemap_shards"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("sitemap_batches.id", ondelete="CASCADE"), index=True
    )
    shard_order: Mapped[int] = mapped_column(Integer)
    loc: Mapped[str] = mapped_column(String(300))
    source_file: Mapped[str] = mapped_column(String(120))
    compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    depth: Mapped[int] = mapped_column(Integer, default=1)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    # 读完即落的原始 url 记录：续跑时直接回放，done 分片不重读、不重复
    raw_records: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    skipped: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
