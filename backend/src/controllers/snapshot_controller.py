"""sitemap 快照控制器：批次展开/续跑/激活/查询、待复核裁决，全部带审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.site import Site
from src.models.sitemap_url_record import SitemapUrlRecord
from src.services import audit_service, snapshot_service
from src.utils.errors import not_found


def list_batches(db: Session, **filters) -> dict:
    return snapshot_service.list_batches(db, **filters)


def get_batch(db: Session, batch_id: int) -> dict:
    batch = snapshot_service.get_batch(db, batch_id)
    return snapshot_service.batch_detail(db, batch)


def create_batch(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    site = db.get(Site, int(payload["site_id"]))
    if site is None:
        raise not_found("site", payload["site_id"])
    batch = snapshot_service.expand_site(db, site, created_by=principal["email"])
    if batch.status == snapshot_service.BATCH_READY:
        # 展开成功即激活：主表切换与批次状态在同一事务，失败整体回滚到旧版
        snapshot_service.activate_batch(db, batch)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="snapshot.expand",
        entity="sitemap_batch",
        entity_id=batch.id,
        detail={
            "site_code": site.code,
            "batch_no": batch.batch_no,
            "status": batch.status,
            "shards": batch.shard_total,
            "conflicts": batch.conflict_total,
            "needs_review": batch.needs_review_total,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return snapshot_service.batch_detail(db, batch)


def resume_batch(db: Session, batch_id: int, principal: dict, request: Request) -> dict:
    batch = snapshot_service.resume_batch(db, batch_id)
    if batch.status == snapshot_service.BATCH_READY:
        snapshot_service.activate_batch(db, batch)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="snapshot.resume",
        entity="sitemap_batch",
        entity_id=batch.id,
        detail={
            "batch_no": batch.batch_no,
            "status": batch.status,
            "shard_done": batch.shard_done,
            "shard_failed": batch.shard_failed,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return snapshot_service.batch_detail(db, batch)


def list_reviews(db: Session, *, site_id: int | None = None, review_status: str | None = None) -> dict:
    rows = snapshot_service.list_review_queue(db, site_id=site_id, review_status=review_status)
    sites = {site.id: site for site in db.scalars(select(Site)).all()}
    return {
        "items": [snapshot_service.serialize_record(row, sites.get(row.site_id)) for row in rows],
        "total": len(rows),
    }


def resolve_review(
    db: Session, record_id: int, payload: dict, principal: dict, request: Request
) -> dict:
    record = db.get(SitemapUrlRecord, record_id)
    if record is None:
        raise not_found("sitemap_url_record", record_id)
    snapshot_service.resolve_review(
        db,
        record,
        lastmod=payload["lastmod"],
        reviewer=principal["email"],
        note=payload.get("note"),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="snapshot.review",
        entity="sitemap_url_record",
        entity_id=record.id,
        detail={
            "canonical_url": record.canonical_url,
            "lastmod": record.reviewed_lastmod,
            "batch_id": record.batch_id,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    site = db.get(Site, record.site_id)
    return snapshot_service.serialize_record(record, site)
