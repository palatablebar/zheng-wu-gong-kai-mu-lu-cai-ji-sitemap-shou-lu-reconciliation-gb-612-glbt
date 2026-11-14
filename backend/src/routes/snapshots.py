from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CRAWL_CREATE_ROLES, DEFECT_REVIEW_ROLES
from src.controllers import snapshot_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.snapshot import SnapshotBatchCreateIn, SnapshotReviewIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/sitemap-snapshots", tags=["sitemap-snapshots"])


@router.get("")
def list_batches(
    site_id: int | None = None,
    status: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return snapshot_controller.list_batches(
        db, site_id=site_id, status=status, limit=page.limit, offset=page.offset
    )


@router.get("/reviews")
def list_reviews(
    site_id: int | None = None,
    review_status: str | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return snapshot_controller.list_reviews(db, site_id=site_id, review_status=review_status)


@router.get("/{batch_id}")
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return snapshot_controller.get_batch(db, batch_id)


@router.post("", status_code=201)
def create_batch(
    payload: SnapshotBatchCreateIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CRAWL_CREATE_ROLES)),
) -> dict:
    return snapshot_controller.create_batch(db, payload.model_dump(), principal, request)


@router.post("/{batch_id}/resume", status_code=201)
def resume_batch(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CRAWL_CREATE_ROLES)),
) -> dict:
    return snapshot_controller.resume_batch(db, batch_id, principal, request)


@router.patch("/reviews/{record_id}")
def resolve_review(
    record_id: int,
    payload: SnapshotReviewIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*DEFECT_REVIEW_ROLES)),
) -> dict:
    return snapshot_controller.resolve_review(db, record_id, payload.model_dump(), principal, request)
