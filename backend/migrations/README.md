# 数据库迁移说明

本项目用 SQLAlchemy `Base.metadata.create_all()` 在 backend 启动时建表（见 `src/config/app.py`），
因此首次 `docker compose up` 会自动建好表，不需要额外执行迁移。

- `001_core_tables.sql`：核心表的人类可读 DDL 参考（MySQL 8.0 语法），用于 reviewer 对照字段；
  运行时不执行它。
- `002_sitemap_snapshot.sql`：sitemap 索引快照三张新表（`sitemap_batches` / `sitemap_shards` /
  `sitemap_url_records`）与 `sitemap_entries`、`crawl_runs` 的补列参考，同样不直接执行。
  新库由 `create_all` 一次建好；**老库由 `src/config/migrations.py::ensure_extra_columns`
  在启动时幂等补列**（已存在的列跳过），不需要手工 ALTER。
- 后续如果要做字段变更，建议引入 Alembic，并在本目录按 `00x_<描述>.py` 递增。
- seed 数据只走 `backend/scripts/seed_demo.py`，不要写进 SQL。
