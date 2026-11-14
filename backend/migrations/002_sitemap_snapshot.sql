-- 002：sitemap 索引快照（可恢复展开）三张新表 + 主表/巡检表新列。
-- 参考用 MySQL 8.0 语法；实际建表由 SQLAlchemy create_all 完成，
-- 已存在的库由 app 启动时的 ensure_column 幂等补列（见 config/app.py）。

CREATE TABLE sitemap_batches (
    id                    INT AUTO_INCREMENT PRIMARY KEY,
    site_id               INT NOT NULL,
    batch_no              VARCHAR(60) NOT NULL UNIQUE,
    status                VARCHAR(20) NOT NULL DEFAULT 'expanding',
    index_url             VARCHAR(300) NOT NULL,
    snapshot_fingerprint  VARCHAR(64),
    shard_total           INT NOT NULL DEFAULT 0,
    shard_done            INT NOT NULL DEFAULT 0,
    shard_failed          INT NOT NULL DEFAULT 0,
    record_total          INT NOT NULL DEFAULT 0,
    canonical_total       INT NOT NULL DEFAULT 0,
    conflict_total        INT NOT NULL DEFAULT 0,
    needs_review_total    INT NOT NULL DEFAULT 0,
    review_open_total     INT NOT NULL DEFAULT 0,
    superseded_batch_id   INT NULL,
    last_error            TEXT,
    stats                 JSON,
    created_by            VARCHAR(160) NOT NULL DEFAULT 'system',
    started_at            DATETIME NOT NULL,
    finished_at           DATETIME,
    activated_at          DATETIME,
    created_at            DATETIME NOT NULL,
    updated_at            DATETIME NOT NULL,
    CONSTRAINT fk_batch_site FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
    CONSTRAINT fk_batch_prev FOREIGN KEY (superseded_batch_id) REFERENCES sitemap_batches(id) ON DELETE SET NULL,
    INDEX ix_batch_site (site_id),
    INDEX ix_batch_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sitemap_shards (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    batch_id        INT NOT NULL,
    shard_order     INT NOT NULL,
    loc             VARCHAR(300) NOT NULL,
    source_file     VARCHAR(120) NOT NULL,
    compressed      TINYINT(1) NOT NULL DEFAULT 0,
    depth           INT NOT NULL DEFAULT 1,
    content_sha256  VARCHAR(64),
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    record_count    INT NOT NULL DEFAULT 0,
    raw_records     JSON,
    error           TEXT,
    skipped         JSON,
    read_at         DATETIME,
    created_at      DATETIME NOT NULL,
    updated_at      DATETIME NOT NULL,
    CONSTRAINT fk_shard_batch FOREIGN KEY (batch_id) REFERENCES sitemap_batches(id) ON DELETE CASCADE,
    INDEX ix_shard_batch (batch_id),
    INDEX ix_shard_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sitemap_url_records (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    batch_id         INT NOT NULL,
    site_id          INT NOT NULL,
    loc              VARCHAR(300) NOT NULL,
    canonical_url    VARCHAR(300) NOT NULL,
    lastmod          VARCHAR(30),
    changefreq       VARCHAR(20),
    priority         DOUBLE,
    source_file      VARCHAR(120) NOT NULL,
    decision_basis   VARCHAR(30) NOT NULL DEFAULT 'single_source',
    needs_review     TINYINT(1) NOT NULL DEFAULT 0,
    review_status    VARCHAR(20) NOT NULL DEFAULT 'none',
    review_reasons   JSON,
    source_count     INT NOT NULL DEFAULT 1,
    duplicate        TINYINT(1) NOT NULL DEFAULT 0,
    lastmod_values   JSON,
    sources          JSON,
    reviewer         VARCHAR(160),
    review_note      TEXT,
    reviewed_lastmod VARCHAR(30),
    reviewed_at      DATETIME,
    created_at       DATETIME NOT NULL,
    updated_at       DATETIME NOT NULL,
    CONSTRAINT uq_sitemap_url_batch_canonical UNIQUE (batch_id, canonical_url),
    CONSTRAINT fk_url_batch FOREIGN KEY (batch_id) REFERENCES sitemap_batches(id) ON DELETE CASCADE,
    CONSTRAINT fk_url_site FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
    INDEX ix_url_canonical (canonical_url),
    INDEX ix_url_review (needs_review, review_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 既有表补列
ALTER TABLE sitemap_entries
    ADD COLUMN batch_id INT NULL AFTER site_id,
    ADD COLUMN decision_basis VARCHAR(30) NOT NULL DEFAULT 'single_source' AFTER source_file,
    ADD COLUMN needs_review TINYINT(1) NOT NULL DEFAULT 0 AFTER decision_basis,
    ADD COLUMN review_status VARCHAR(20) NOT NULL DEFAULT 'none' AFTER needs_review,
    ADD CONSTRAINT fk_entry_batch FOREIGN KEY (batch_id) REFERENCES sitemap_batches(id) ON DELETE SET NULL,
    ADD INDEX ix_entry_batch (batch_id),
    ADD INDEX ix_entry_review (needs_review);

ALTER TABLE crawl_runs
    ADD COLUMN snapshot_fingerprint VARCHAR(64) NULL AFTER seed_key,
    ADD COLUMN snapshot_batch_ids JSON NULL AFTER snapshot_fingerprint,
    ADD INDEX ix_run_fingerprint (snapshot_fingerprint);
