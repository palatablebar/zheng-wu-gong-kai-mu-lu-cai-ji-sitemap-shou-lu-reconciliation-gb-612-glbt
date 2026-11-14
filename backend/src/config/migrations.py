"""轻量幂等迁移：create_all 建不出的「既有表新列」在这里补。

只处理新增可空/带默认值的列与索引，不做破坏性变更；MySQL 与 PostgreSQL 都支持。
启动时执行一次，已存在的列跳过，保证老库（001 结构）能平滑升级到快照结构。
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# 表 -> [(列名, DDL 类型片段)]
_EXTRA_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "sitemap_entries": [
        ("batch_id", "INTEGER NULL"),
        ("decision_basis", "VARCHAR(30) NOT NULL DEFAULT 'single_source'"),
        ("needs_review", "BOOLEAN NOT NULL DEFAULT 0"),
        ("review_status", "VARCHAR(20) NOT NULL DEFAULT 'none'"),
    ],
    "crawl_runs": [
        ("snapshot_fingerprint", "VARCHAR(64) NULL"),
        ("snapshot_batch_ids", "JSON NULL"),
    ],
}


def ensure_extra_columns(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied: list[str] = []
    with engine.begin() as conn:
        for table, columns in _EXTRA_COLUMNS.items():
            if table not in existing_tables:
                continue  # 新库由 create_all 一次性建好
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                applied.append(f"{table}.{name}")
    return applied
