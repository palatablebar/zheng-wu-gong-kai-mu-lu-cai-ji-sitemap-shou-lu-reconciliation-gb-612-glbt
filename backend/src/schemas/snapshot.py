from __future__ import annotations

from pydantic import BaseModel, Field


class SnapshotBatchCreateIn(BaseModel):
    site_id: int = Field(ge=1)


class SnapshotReviewIn(BaseModel):
    lastmod: str = Field(min_length=10, max_length=30, pattern=r"^\d{4}-\d{2}-\d{2}")
    note: str | None = Field(default=None, max_length=500)
