"""快照服务集成测试：可恢复展开、断点续跑不重复、事务化激活失败回滚、待复核裁决、巡检门控。

用 SQLite 内存库跑真实 SQLAlchemy 模型与服务代码；fixture 站写在 tmp_path，
不依赖 MySQL/Redis/网络。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.constants.demo_data import SITES
from src.models import (
    Base,
    CatalogItem,
    CrawlRun,
    Defect,
    Site,
    SitemapBatch,
    SitemapEntry,
    SitemapShard,
    SitemapUrlRecord,
)
from src.services import crawl_run_service, fixture_site, snapshot_service
from src.utils.errors import AppError


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEMAP_FIXTURE_DIR", str(tmp_path))
    # fixture_site 在导入时缓存了 FIXTURE_BASE，强制指向临时目录
    monkeypatch.setattr(fixture_site, "FIXTURE_BASE", tmp_path)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    yield session
    session.close()


def _make_site(db, spec) -> Site:
    site = Site(
        code=spec["code"],
        name=spec["name"],
        home_url=spec["home_url"],
        sitemap_url=spec["sitemap_url"],
        page_size=12,
        max_depth=3,
        enabled=True,
    )
    db.add(site)
    db.flush()
    for row in fixture_site.catalog_rows(spec):
        db.add(
            CatalogItem(
                site_id=site.id,
                column_code=row["column_code"],
                column_name=row["column_name"],
                entry_url=row["entry_url"],
                canonical_url=row["canonical_url"],
                page_no=row["page_no"],
                item_no=row["item_no"],
                depth=2,
                discovered_from=row["discovered_from"],
                title=row["title"],
            )
        )
    db.flush()
    return site


def test_expand_then_activate_materializes_main_table(db_session):
    spec = SITES[0]
    site = _make_site(db_session, spec)
    batch = snapshot_service.expand_site(db_session, site, created_by="analyst@x")

    assert batch.status == snapshot_service.BATCH_READY
    assert batch.shard_total == 3 and batch.shard_done == 3 and batch.shard_failed == 0
    assert batch.conflict_total == 4
    assert batch.needs_review_total == 1 and batch.review_open_total == 1
    assert batch.snapshot_fingerprint and len(batch.snapshot_fingerprint) == 64
    # ready 阶段半批结果不进主表
    assert db_session.scalar(select(func.count()).select_from(SitemapEntry)) == 0

    snapshot_service.activate_batch(db_session, batch)
    assert batch.status == snapshot_service.BATCH_ACTIVE
    entries = db_session.scalars(select(SitemapEntry)).all()
    assert len(entries) == 121
    assert all(entry.batch_id == batch.id for entry in entries)
    assert sum(1 for entry in entries if entry.needs_review) == 1


def test_resume_does_not_reread_done_shards_or_duplicate(db_session):
    spec = SITES[1]
    site = _make_site(db_session, spec)
    batch = snapshot_service.create_batch(db_session, site, created_by="analyst@x")
    shards = list(
        db_session.scalars(
            select(SitemapShard).where(SitemapShard.batch_id == batch.id).order_by(SitemapShard.shard_order)
        )
    )
    assert len(shards) == 3
    # 模拟崩溃：前两个分片已落检查点，第三个失败
    shards[0].status = snapshot_service.SHARD_DONE
    shards[0].raw_records = [
        {"loc": f"{spec['home_url']}/x.html", "canonical_url": f"{spec['home_url']}/x.html",
         "lastmod": "2026-10-01", "source_file": shards[0].source_file, "shard_order": 1}
    ]
    shards[0].record_count = 1
    shards[1].status = snapshot_service.SHARD_FAILED
    shards[1].error = "io error"
    db_session.flush()

    resumed = snapshot_service.resume_batch(db_session, batch.id)
    assert resumed.status == snapshot_service.BATCH_READY
    assert resumed.shard_done == 3 and resumed.shard_failed == 0
    # 合并记录每个 canonical 唯一，无重复写入
    records = db_session.scalars(
        select(SitemapUrlRecord).where(SitemapUrlRecord.batch_id == batch.id)
    ).all()
    canonicals = [record.canonical_url for record in records]
    assert len(canonicals) == len(set(canonicals))
    # 再次续跑已完成批次应被拒绝
    with pytest.raises(AppError) as exc:
        snapshot_service.resume_batch(db_session, batch.id)
    assert exc.value.code == "CONFLICT"


def test_activation_failure_keeps_previous_active_batch(db_session):
    spec_a, spec_b = SITES[0], SITES[2]
    site_a = _make_site(db_session, spec_a)
    first = snapshot_service.expand_site(db_session, site_a, created_by="x")
    snapshot_service.activate_batch(db_session, first)

    site_b = _make_site(db_session, spec_b)
    second = snapshot_service.expand_site(db_session, site_b, created_by="x")
    assert second.status == snapshot_service.BATCH_READY

    # 让激活中途抛错：模拟事务回滚后，旧 active 仍是 first
    original = snapshot_service.activate_batch

    def boom(db, batch):
        raise RuntimeError("commit failed")

    snapshot_service.activate_batch = boom
    try:
        with pytest.raises(RuntimeError):
            snapshot_service.activate_batch(db_session, second)
    finally:
        snapshot_service.activate_batch = original

    active = db_session.scalar(
        select(SnapshotBatch).where(SnapshotBatch.status == snapshot_service.BATCH_ACTIVE)
    )
    assert active.id == first.id
    # 主表仍只有第一批的条目
    assert db_session.scalar(select(func.count()).select_from(SitemapEntry)) == 121


def test_review_resolution_syncs_to_active_main_table(db_session):
    spec = SITES[0]
    site = _make_site(db_session, spec)
    batch = snapshot_service.expand_site(db_session, site, created_by="admin@x")
    snapshot_service.activate_batch(db_session, batch)
    record = db_session.scalar(
        select(SitemapUrlRecord).where(
            SitemapUrlRecord.batch_id == batch.id, SitemapUrlRecord.needs_review.is_(True)
        )
    )
    assert record is not None
    assert len(record.sources) >= 2

    with pytest.raises(AppError) as exc:
        snapshot_service.resolve_review(db_session, record, lastmod="bad", reviewer="a", note=None)
    assert exc.value.code == "VALIDATION"

    snapshot_service.resolve_review(db_session, record, lastmod="2026-08-08", reviewer="admin@x", note="核定")
    assert record.review_status == snapshot_service.REVIEW_RESOLVED
    entry = db_session.scalar(
        select(SitemapEntry).where(
            SitemapEntry.canonical_url == record.canonical_url, SitemapEntry.batch_id == batch.id
        )
    )
    assert entry.lastmod == "2026-08-08" and entry.needs_review is False
    assert batch.review_open_total == 0


def test_corrupt_shard_keeps_batch_expanding_then_resume_recovers(db_session, tmp_path):
    spec = SITES[0]
    site = _make_site(db_session, spec)
    # 先正常生成 fixture，再把第二个分片写坏，模拟「读到一半磁盘损坏」
    fixture_site.ensure_site_fixture(spec)
    root = fixture_site.fixture_root(spec["code"])
    target = root / fixture_site.SHARD_FILES[1]
    good_bytes = target.read_bytes()
    target.write_bytes(b"<?xml version='1.0'?><urlset><url><loc>")

    batch = snapshot_service.expand_site(db_session, site, created_by="analyst@x")
    assert batch.status == snapshot_service.BATCH_EXPANDING
    assert batch.shard_total == 3 and batch.shard_failed == 1 and batch.shard_done == 2
    # 半批不进主表
    assert db_session.scalar(select(func.count()).select_from(SitemapEntry)) == 0
    failed_shard = db_session.scalar(
        select(SitemapShard).where(
            SitemapShard.batch_id == batch.id, SitemapShard.status == snapshot_service.SHARD_FAILED
        )
    )
    assert failed_shard is not None and failed_shard.error

    # 修复分片后续跑：done 分片不重读，failed 分片重试成功
    target.write_bytes(good_bytes)
    resumed = snapshot_service.resume_batch(db_session, batch.id)
    assert resumed.status == snapshot_service.BATCH_READY
    assert resumed.shard_done == 3 and resumed.shard_failed == 0
    snapshot_service.activate_batch(db_session, resumed)
    assert db_session.scalar(select(func.count()).select_from(SitemapEntry)) == 121


def test_crawl_run_uses_one_snapshot_and_keeps_history_on_failure(db_session):
    spec = SITES[0]
    site = _make_site(db_session, spec)

    run = CrawlRun(run_no="C20261114-0001", trigger="manual", status="queued", operator_email="analyst@x")
    db_session.add(run)
    crawl_run_service.execute_run(db_session, run, [site])
    assert run.status == "success"
    assert run.snapshot_fingerprint and len(run.snapshot_batch_ids) == 1
    first_defects = db_session.scalar(
        select(func.count()).select_from(Defect).where(Defect.run_id == run.id)
    )
    assert first_defects == 13

    # 快照阶段故障：整次运行 failed，覆盖率/缺陷为 0，历史缺陷原样保留
    original = snapshot_service.ensure_active_snapshot

    def boom(db, site, created_by):
        raise RuntimeError("snapshot exploded")

    snapshot_service.ensure_active_snapshot = boom
    failed_run = CrawlRun(run_no="C20261114-0002", trigger="manual", status="queued", operator_email="analyst@x")
    db_session.add(failed_run)
    crawl_run_service.execute_run(db_session, failed_run, [site])
    snapshot_service.ensure_active_snapshot = original

    assert failed_run.status == "failed"
    assert failed_run.coverage == 0 and failed_run.defect_count == 0
    total_defects = db_session.scalar(select(func.count()).select_from(Defect))
    assert total_defects == first_defects
    # 指纹门控：不一致直接拒绝
    with pytest.raises(AppError) as exc:
        snapshot_service.verify_run_fingerprint(db_session, list(run.snapshot_batch_ids.values()), "wrong")
    assert exc.value.code == "SNAPSHOT_MISMATCH"


def test_second_crawl_reuses_same_fingerprint_when_fixture_unchanged(db_session):
    spec = SITES[0]
    site = _make_site(db_session, spec)
    run_one = CrawlRun(run_no="C20261114-0003", trigger="manual", status="queued", operator_email="a@x")
    db_session.add(run_one)
    crawl_run_service.execute_run(db_session, run_one, [site])
    run_two = CrawlRun(run_no="C20261114-0004", trigger="schedule", status="queued", operator_email="scheduler@x")
    db_session.add(run_two)
    crawl_run_service.execute_run(db_session, run_two, [site])
    assert run_one.snapshot_fingerprint == run_two.snapshot_fingerprint
    # 不会产生第二个 active 批次
    active_count = db_session.scalar(
        select(func.count()).select_from(SnapshotBatch).where(SnapshotBatch.status == "active")
    )
    assert active_count == 1
