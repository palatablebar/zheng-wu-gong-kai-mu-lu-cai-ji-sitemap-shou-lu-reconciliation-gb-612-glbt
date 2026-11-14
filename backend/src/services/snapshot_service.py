"""sitemap 快照服务：可恢复的索引展开、事务化激活、待复核裁决。

核心约束（对应巡检批次口径）：
1. **逐层展开 + 断点续跑不重复**：索引读一次生成分片检查点，分片逐个读，
   done 的分片永不重读、不重复写记录；failed/pending 才重试；
2. **半批结果不进主表**：expanding/ready/failed 批次的记录只存在
   ``sitemap_url_records``，只有 active 批次会在同一事务内替换 ``sitemap_entries``；
3. **失败恢复旧版**：激活失败整事务回滚，旧 active 批次继续是巡检唯一快照；
4. **同一批快照**：批次指纹 = 全分片内容哈希，crawl_run 记录指纹并在采集/合并/
   探测/对账前校验，不一致直接拒绝；
5. **不删历史**：批次、分片、URL 记录、缺陷、整改记录都只追加/更新，不物理删除。
"""
from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.demo_data import SITES as SITE_SPECS
from src.models.site import Site
from src.models.sitemap_batch import SnapshotBatch
from src.models.sitemap_entry import SitemapEntry
from src.models.sitemap_shard import SitemapShard
from src.models.sitemap_url_record import SitemapUrlRecord
from src.services import fixture_site, sitemap_merge, sitemap_reader
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, local_now, utcnow

SPEC_BY_CODE = {spec["code"]: spec for spec in SITE_SPECS}

# 批次/复核状态（前后端枚举同步点：frontend/site/js/config.js）
BATCH_EXPANDING = "expanding"
BATCH_READY = "ready"
BATCH_ACTIVE = "active"
BATCH_FAILED = "failed"
BATCH_ARCHIVED = "archived"

SHARD_PENDING = "pending"
SHARD_DONE = "done"
SHARD_FAILED = "failed"

REVIEW_OPEN = "open"
REVIEW_RESOLVED = "resolved"
REVIEW_NONE = "none"

BATCH_LABELS = {
    BATCH_EXPANDING: "展开中",
    BATCH_READY: "待激活",
    BATCH_ACTIVE: "已激活",
    BATCH_FAILED: "失败",
    BATCH_ARCHIVED: "已归档",
}
BASIS_LABELS = {
    sitemap_merge.BASIS_SINGLE_SOURCE: "单一来源，无需裁决",
    sitemap_merge.BASIS_LASTMOD_LATEST: "取最新 lastmod",
    sitemap_merge.BASIS_SHARD_ORDER_MIN: "lastmod 并列，取序号最小分片",
    sitemap_merge.BASIS_LOC_LEX_MIN: "lastmod 与分片均并列，取 loc 字典序最小",
    sitemap_merge.BASIS_REVIEW_PENDING: "存在无法解析的 lastmod，待人工复核",
}


def next_batch_no(db: Session, site: Site) -> str:
    prefix = f"S{local_now():%Y%m%d}-{site.code}"
    count = db.scalar(
        select(func.count()).select_from(SnapshotBatch).where(SnapshotBatch.batch_no.like(f"{prefix}%"))
    ) or 0
    return f"{prefix}-{count + 1:03d}"


def _shard_value(shard, name: str):
    if isinstance(shard, dict):
        return shard.get(name)
    return getattr(shard, name)


def _fingerprint(shards: list) -> str:
    """快照指纹：按分片序号排序，逐段哈希「序号 + 内容 sha256」。

    入参可以是 ORM 分片对象，也可以是 flatten_index 产出的 dict（只要带
    shard_order 与 content_sha256），保证「磁盘当前内容」与「已落库批次」
    用完全相同的算法计算，指纹相等才允许复用批次。
    """
    digest = hashlib.sha256()
    ordered = sorted(shards, key=lambda item: int(_shard_value(item, "shard_order")))
    for shard in ordered:
        digest.update(str(int(_shard_value(shard, "shard_order"))).encode())
        digest.update(str(_shard_value(shard, "content_sha256") or "").encode())
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 1. 可恢复展开
# --------------------------------------------------------------------------- #
def create_batch(db: Session, site: Site, *, created_by: str) -> SnapshotBatch:
    """登记一个 expanding 批次并生成索引分片检查点（不读任何叶子分片）。"""
    spec = SPEC_BY_CODE.get(site.code)
    root = fixture_site.fixture_root(site.code)
    if spec is not None:
        fixture_site.ensure_site_fixture(spec)
    loader = sitemap_reader.SitemapFixture(root)
    index_url = site.sitemap_url or f"{site.home_url.rstrip('/')}/sitemap.xml"
    plan = sitemap_reader.flatten_index(loader, site.home_url, index_url)

    batch = SnapshotBatch(
        site_id=site.id,
        batch_no=next_batch_no(db, site),
        status=BATCH_EXPANDING,
        index_url=plan["index_url"],
        created_by=created_by,
        stats={"skipped": plan["skipped"]},
    )
    db.add(batch)
    db.flush()
    failed_seeds = 0
    for shard in plan["shards"]:
        parse_error = shard.get("parse_error")
        db.add(
            SitemapShard(
                batch_id=batch.id,
                shard_order=shard["shard_order"],
                loc=shard["loc"],
                source_file=shard["source_file"],
                compressed=shard["compressed"],
                depth=shard["depth"],
                content_sha256=shard["content_sha256"],
                status=SHARD_FAILED if parse_error else SHARD_PENDING,
                error=parse_error,
            )
        )
        if parse_error:
            failed_seeds += 1
    batch.shard_total = len(plan["shards"])
    batch.shard_failed = failed_seeds
    if not plan["shards"]:
        batch.status = BATCH_FAILED
        batch.last_error = "索引未展开出任何同站分片：" + ";".join(
            f"{item.get('url')}({item.get('reason')})" for item in plan["skipped"]
        )
        batch.finished_at = utcnow()
    elif failed_seeds:
        # 有损坏分片：批次保持 expanding，等待断点续跑重试（半批不激活、不进主表）
        batch.status = BATCH_EXPANDING
        batch.last_error = "存在解析失败分片，等待续跑：" + ";".join(
            f"{s.get('source_file')}={s.get('parse_error')}" for s in plan["shards"] if s.get("parse_error")
        )
    db.flush()
    return batch


def _materialize_merge(db: Session, batch: SnapshotBatch, records: list[dict]) -> dict:
    result = sitemap_merge.merge_records(records)
    site_id = batch.site_id
    for canonical, conclusion in result["merged"].items():
        db.add(
            SitemapUrlRecord(
                batch_id=batch.id,
                site_id=site_id,
                loc=conclusion["loc"],
                canonical_url=canonical,
                lastmod=conclusion["lastmod"],
                changefreq=conclusion["changefreq"],
                priority=conclusion["priority"],
                source_file=conclusion["source_file"],
                decision_basis=conclusion["decision_basis"],
                needs_review=conclusion["needs_review"],
                review_status=REVIEW_OPEN if conclusion["needs_review"] else REVIEW_NONE,
                review_reasons=conclusion["review_reasons"],
                source_count=conclusion["source_count"],
                duplicate=conclusion["duplicate"],
                lastmod_values=conclusion["lastmod_values"],
                sources=conclusion["sources"],
            )
        )
    stats = result["stats"]
    batch.record_total = stats["url_total"]
    batch.canonical_total = stats["canonical_total"]
    batch.conflict_total = stats["conflict_total"]
    batch.needs_review_total = stats["needs_review_total"]
    batch.review_open_total = stats["needs_review_total"]
    batch.stats = {**(batch.stats or {}), "merge": stats}
    return stats


def expand_batch(db: Session, batch: SnapshotBatch) -> SnapshotBatch:
    """读完全部未完成分片并合并；可反复调用，done 分片不重读、不重复。"""
    site = db.get(Site, batch.site_id)
    if site is None:
        raise not_found("site", batch.site_id)
    root = fixture_site.fixture_root(site.code)
    loader = sitemap_reader.SitemapFixture(root)
    shards = list(
        db.scalars(
            select(SitemapShard)
            .where(SitemapShard.batch_id == batch.id)
            .order_by(SitemapShard.shard_order)
        ).all()
    )
    if not shards:
        batch.status = BATCH_FAILED
        batch.last_error = "批次没有任何分片检查点"
        batch.finished_at = utcnow()
        db.flush()
        return batch

    all_records: list[dict] = []
    for shard in shards:
        if shard.status == SHARD_DONE:
            # 断点续跑：直接回放该分片已落库的原始记录，不重新抓取、不重复写入
            all_records.extend(list(shard.raw_records or []))
            continue
        out = sitemap_reader.read_shard(
            loader, {"loc": shard.loc, "source_file": shard.source_file, "shard_order": shard.shard_order}
        )
        if out["error"]:
            shard.status = SHARD_FAILED
            shard.error = out["error"]
            shard.content_sha256 = out["content_sha256"]
            continue
        all_records.extend(out["records"])
        shard.status = SHARD_DONE
        shard.error = None
        shard.record_count = len(out["records"])
        shard.raw_records = out["records"]
        shard.content_sha256 = out["content_sha256"]
        shard.read_at = utcnow()
        db.flush()

    done = [shard for shard in shards if shard.status == SHARD_DONE]
    failed = [shard for shard in shards if shard.status == SHARD_FAILED]
    batch.shard_done = len(done)
    batch.shard_failed = len(failed)

    if failed:
        batch.status = BATCH_EXPANDING  # 保持可续跑，半批结果不激活、不进主表
        batch.last_error = "分片读取失败：" + ";".join(f"{s.source_file}={s.error}" for s in failed)
        batch.finished_at = utcnow()
        db.flush()
        return batch

    # 全部分片成功：清掉上一次续跑可能残留的合并结果，再做一次确定性合并
    for old in db.scalars(select(SitemapUrlRecord).where(SitemapUrlRecord.batch_id == batch.id)).all():
        db.delete(old)
    db.flush()
    _materialize_merge(db, batch, all_records)
    batch.snapshot_fingerprint = _fingerprint(done)
    batch.last_error = None
    batch.status = BATCH_READY
    batch.finished_at = utcnow()
    db.flush()
    return batch


def expand_site(db: Session, site: Site, *, created_by: str) -> SnapshotBatch:
    """新建批次并立即展开（同步路径用）；失败返回 failed/expanding 批次，不抛异常。"""
    batch = create_batch(db, site, created_by=created_by)
    if batch.status == BATCH_FAILED:
        return batch
    return expand_batch(db, batch)


def resume_batch(db: Session, batch_id: int) -> SnapshotBatch:
    """断点续跑入口：只接受未结束的批次。"""
    batch = db.get(SnapshotBatch, batch_id)
    if batch is None:
        raise not_found("sitemap_batch", batch_id)
    if batch.status not in (BATCH_EXPANDING, BATCH_FAILED):
        raise AppError("CONFLICT", 409, {"batch_no": batch.batch_no, "status": batch.status})
    return expand_batch(db, batch)


# --------------------------------------------------------------------------- #
# 2. 事务化激活（失败回滚旧版，半批不进覆盖率/导出）
# --------------------------------------------------------------------------- #
def activate_batch(db: Session, batch: SnapshotBatch) -> SnapshotBatch:
    if batch.status != BATCH_READY:
        raise AppError("CONFLICT", 409, {"batch_no": batch.batch_no, "status": batch.status})
    previous = db.scalar(
        select(SnapshotBatch)
        .where(SnapshotBatch.site_id == batch.site_id, SnapshotBatch.status == BATCH_ACTIVE)
        .order_by(SnapshotBatch.id.desc())
    )
    # 同一事务内切换主表：先删旧条目，再按本批合并记录重写；任何异常整体回滚
    old_entries = db.scalars(select(SitemapEntry).where(SitemapEntry.site_id == batch.site_id)).all()
    for entry in old_entries:
        db.delete(entry)
    records = db.scalars(
        select(SitemapUrlRecord)
        .where(SitemapUrlRecord.batch_id == batch.id)
        .order_by(SitemapUrlRecord.canonical_url)
    ).all()
    for record in records:
        db.add(
            SitemapEntry(
                site_id=batch.site_id,
                batch_id=batch.id,
                loc=record.loc,
                canonical_url=record.canonical_url,
                lastmod=record.reviewed_lastmod or record.lastmod,
                changefreq=record.changefreq,
                priority=record.priority if record.priority is not None else 0.8,
                source_file=record.source_file,
                decision_basis=record.decision_basis,
                needs_review=record.needs_review and record.review_status == REVIEW_OPEN,
                review_status=record.review_status,
            )
        )
    if previous is not None and previous.id != batch.id:
        previous.status = BATCH_ARCHIVED
        batch.superseded_batch_id = previous.id
    batch.status = BATCH_ACTIVE
    batch.activated_at = utcnow()
    db.flush()
    return batch


def ensure_active_snapshot(db: Session, site: Site, *, created_by: str) -> SnapshotBatch:
    """巡检门控：确保站点有一个与当前 fixture 指纹一致的 active 批次。

    - 已有 active 且指纹与当前分片一致 -> 直接复用（同一批快照）；
    - 否则展开新批次；只有成功激活才返回，失败时旧 active 原样保留并抛错，
      调用方据此让整次巡检失败，半批结果不会进入覆盖率或导出。
    """
    root = fixture_site.fixture_root(site.code)
    loader = sitemap_reader.SitemapFixture(root)
    index_url = site.sitemap_url or f"{site.home_url.rstrip('/')}/sitemap.xml"
    plan = sitemap_reader.flatten_index(loader, site.home_url, index_url)
    # 与已落库批次同一个算法、同一份分片内容哈希，才能可靠判断「快照未变」
    current_fingerprint = _fingerprint(plan["shards"])

    active = db.scalar(
        select(SnapshotBatch)
        .where(SnapshotBatch.site_id == site.id, SnapshotBatch.status == BATCH_ACTIVE)
        .order_by(SnapshotBatch.id.desc())
    )
    if active is not None and active.snapshot_fingerprint == current_fingerprint:
        return active

    batch = expand_site(db, site, created_by=created_by)
    if batch.status != BATCH_READY:
        raise AppError(
            "SNAPSHOT_INCOMPLETE",
            409,
            {"site_code": site.code, "batch_no": batch.batch_no, "error": batch.last_error},
            message="sitemap 快照未完整展开，已保留旧版快照，本次巡检中止",
        )
    return activate_batch(db, batch)


def verify_run_fingerprint(db: Session, batch_ids: list[int], fingerprint: str | None) -> None:
    """巡检对账前校验：本批所有站点快照指纹一致且仍是 active。"""
    if not batch_ids:
        raise AppError("SNAPSHOT_MISSING", 409, message="巡检缺少 sitemap 快照，拒绝执行对账")
    fingerprints = []
    for batch_id in batch_ids:
        batch = db.get(SnapshotBatch, batch_id)
        if batch is None or batch.status != BATCH_ACTIVE:
            raise AppError("SNAPSHOT_NOT_ACTIVE", 409, {"batch_id": batch_id})
        fingerprints.append(batch.snapshot_fingerprint)
    combined = hashlib.sha256("".join(sorted(f for f in fingerprints if f)).encode()).hexdigest()
    if fingerprint != combined:
        raise AppError(
            "SNAPSHOT_MISMATCH",
            409,
            {"expected": fingerprint, "actual": combined},
            message="分页采集、合并、探测使用的 sitemap 快照不一致，本次结果作废",
        )


# --------------------------------------------------------------------------- #
# 3. 待复核裁决
# --------------------------------------------------------------------------- #
def list_review_queue(db: Session, *, site_id: int | None = None, review_status: str | None = None) -> list[SitemapUrlRecord]:
    stmt = select(SitemapUrlRecord).where(SitemapUrlRecord.needs_review.is_(True))
    if site_id:
        stmt = stmt.where(SitemapUrlRecord.site_id == site_id)
    if review_status:
        stmt = stmt.where(SitemapUrlRecord.review_status == review_status)
    return list(db.scalars(stmt.order_by(SitemapUrlRecord.id)).all())


def resolve_review(
    db: Session,
    record: SitemapUrlRecord,
    *,
    lastmod: str,
    reviewer: str,
    note: str | None,
) -> SitemapUrlRecord:
    """人工裁决 lastmod：校验日期、写裁决留痕，并同步到当前 active 主表条目。"""
    try:
        date.fromisoformat(str(lastmod)[:10])
    except ValueError:
        raise AppError("VALIDATION", 400, {"lastmod": lastmod}, message="裁决 lastmod 必须是 YYYY-MM-DD")
    record.reviewed_lastmod = str(lastmod)[:10]
    record.review_status = REVIEW_RESOLVED
    record.reviewer = reviewer
    record.review_note = note
    record.reviewed_at = utcnow()
    batch = db.get(SnapshotBatch, record.batch_id)
    if batch is not None:
        open_count = db.scalar(
            select(func.count())
            .select_from(SitemapUrlRecord)
            .where(
                SitemapUrlRecord.batch_id == batch.id,
                SitemapUrlRecord.needs_review.is_(True),
                SitemapUrlRecord.review_status == REVIEW_OPEN,
            )
        ) or 0
        batch.review_open_total = open_count
        if batch.status == BATCH_ACTIVE:
            entry = db.scalar(
                select(SitemapEntry).where(
                    SitemapEntry.site_id == record.site_id,
                    SitemapEntry.canonical_url == record.canonical_url,
                    SitemapEntry.batch_id == batch.id,
                )
            )
            if entry is not None:
                entry.lastmod = record.reviewed_lastmod
                entry.needs_review = False
                entry.review_status = REVIEW_RESOLVED
    db.flush()
    return record


# --------------------------------------------------------------------------- #
# 4. 查询序列化
# --------------------------------------------------------------------------- #
def get_batch(db: Session, batch_id: int) -> SnapshotBatch:
    batch = db.get(SnapshotBatch, batch_id)
    if batch is None:
        raise not_found("sitemap_batch", batch_id)
    return batch


def list_batches(
    db: Session,
    *,
    site_id: int | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(SnapshotBatch)
    count_stmt = select(func.count()).select_from(SnapshotBatch)
    if site_id:
        stmt = stmt.where(SnapshotBatch.site_id == site_id)
        count_stmt = count_stmt.where(SnapshotBatch.site_id == site_id)
    if status:
        stmt = stmt.where(SnapshotBatch.status == status)
        count_stmt = count_stmt.where(SnapshotBatch.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(SnapshotBatch.id.desc()).limit(limit).offset(offset)).all()
    sites = {site.id: site for site in db.scalars(select(Site)).all()}
    return {
        "items": [serialize_batch(row, sites.get(row.site_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def serialize_batch(batch: SnapshotBatch, site: Site | None = None) -> dict:
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "site_id": batch.site_id,
        "site_name": site.name if site else None,
        "site_code": site.code if site else None,
        "status": batch.status,
        "status_label": BATCH_LABELS.get(batch.status, batch.status),
        "index_url": batch.index_url,
        "snapshot_fingerprint": batch.snapshot_fingerprint,
        "shard_total": batch.shard_total,
        "shard_done": batch.shard_done,
        "shard_failed": batch.shard_failed,
        "record_total": batch.record_total,
        "canonical_total": batch.canonical_total,
        "conflict_total": batch.conflict_total,
        "needs_review_total": batch.needs_review_total,
        "review_open_total": batch.review_open_total,
        "superseded_batch_id": batch.superseded_batch_id,
        "last_error": batch.last_error,
        "stats": batch.stats or {},
        "created_by": batch.created_by,
        "started_at": iso_utc(batch.started_at),
        "finished_at": iso_utc(batch.finished_at),
        "activated_at": iso_utc(batch.activated_at),
    }


def serialize_shard(shard: SitemapShard) -> dict:
    return {
        "id": shard.id,
        "batch_id": shard.batch_id,
        "shard_order": shard.shard_order,
        "loc": shard.loc,
        "source_file": shard.source_file,
        "compressed": shard.compressed,
        "depth": shard.depth,
        "content_sha256": shard.content_sha256,
        "status": shard.status,
        "record_count": shard.record_count,
        "error": shard.error,
        "read_at": iso_utc(shard.read_at),
    }


def serialize_record(record: SitemapUrlRecord, site: Site | None = None) -> dict:
    return {
        "id": record.id,
        "batch_id": record.batch_id,
        "site_id": record.site_id,
        "site_code": site.code if site else None,
        "loc": record.loc,
        "canonical_url": record.canonical_url,
        "lastmod": record.lastmod,
        "reviewed_lastmod": record.reviewed_lastmod,
        "source_file": record.source_file,
        "decision_basis": record.decision_basis,
        "decision_basis_label": BASIS_LABELS.get(record.decision_basis, record.decision_basis),
        "needs_review": record.needs_review,
        "review_status": record.review_status,
        "review_reasons": record.review_reasons or [],
        "source_count": record.source_count,
        "duplicate": record.duplicate,
        "lastmod_values": record.lastmod_values or [],
        "sources": record.sources or [],
        "reviewer": record.reviewer,
        "review_note": record.review_note,
        "reviewed_at": iso_utc(record.reviewed_at),
    }


def batch_detail(db: Session, batch: SnapshotBatch) -> dict:
    site = db.get(Site, batch.site_id)
    shards = db.scalars(
        select(SitemapShard).where(SitemapShard.batch_id == batch.id).order_by(SitemapShard.shard_order)
    ).all()
    records = db.scalars(
        select(SitemapUrlRecord)
        .where(SitemapUrlRecord.batch_id == batch.id)
        .order_by(SitemapUrlRecord.needs_review.desc(), SitemapUrlRecord.canonical_url)
    ).all()
    payload = serialize_batch(batch, site)
    payload["shards"] = [serialize_shard(shard) for shard in shards]
    payload["records"] = [serialize_record(record, site) for record in records]
    return payload
