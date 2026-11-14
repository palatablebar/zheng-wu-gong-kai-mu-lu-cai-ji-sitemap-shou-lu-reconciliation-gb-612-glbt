"""sitemap 快照批次：一次可恢复的索引展开的完整留痕。

批次生命周期（status）：
- ``expanding`` 展开中：分片逐个落检查点，可断点续跑，半批结果不进 sitemap_entries；
- ``ready``     全分片读完并合并完成，等待激活；
- ``active``    已激活：sitemap_entries 已在同一事务内切到本批，是巡检唯一可用快照；
- ``failed``    展开/激活失败：旧 active 批次保持不变，本批不进覆盖率与导出；
- ``archived``  被更新批次顶替的旧 active 批次（记录保留，不删除）。

``snapshot_fingerprint`` 由全部叶子分片的内容 sha256 排序拼接后再哈希得到，
巡检运行（crawl_runs.snapshot_fingerprint）必须与之一致，证明分页采集、合并、
可达探测、缺陷对账用的是同一批 sitemap 快照。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class SnapshotBatch(Base):
    __tablename__ = "sitemap_batches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    batch_no: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="expanding", index=True)
    index_url: Mapped[str] = mapped_column(String(300), default="")
    snapshot_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    shard_total: Mapped[int] = mapped_column(Integer, default=0)
    shard_done: Mapped[int] = mapped_column(Integer, default=0)
    shard_failed: Mapped[int] = mapped_column(Integer, default=0)
    record_total: Mapped[int] = mapped_column(Integer, default=0)
    canonical_total: Mapped[int] = mapped_column(Integer, default=0)
    conflict_total: Mapped[int] = mapped_column(Integer, default=0)
    needs_review_total: Mapped[int] = mapped_column(Integer, default=0)
    review_open_total: Mapped[int] = mapped_column(Integer, default=0)

    # 激活前一版 active 批次（失败回滚与审计用）
    superseded_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("sitemap_batches.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(160), default="system")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
