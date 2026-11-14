"""离线 fixture 站生成器：把「三个政务站」写成磁盘上的静态 HTML + sitemap + 控制文件。

为什么是 fixture：巡检算法要能在容器内可重复地跑，不依赖外网政务站可达性。
这里生成的不是 mock 数据表，而是**真实的 HTML 文件**（含 rel=next / rel=canonical /
追踪参数链接 / 301 声明 / 404 声明），由 crawl_engine 用 HTML 解析器真解析。

目录结构（容器内 `/app/sample_site/<site_code>/`）：
  index.html                     站点首页，链接到 6 个栏目
  robots.txt                     Sitemap: 声明
  sitemap.xml                    该站 sitemap（含死链）
  status.json                    fixture 侧 HTTP 状态声明（path -> status）
  redirects.json                 fixture 侧 301 声明（path -> 目标 path）
  xxgk/<col>/index.html          栏目第 1 页
  xxgk/<col>/index_<n>.html      栏目第 n 页（n≥2）
  xxgk/<col>/<col>-<nnn>.html    详情页（含 rel=canonical）
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import date, timedelta
from pathlib import Path

from src.constants.demo_data import COLUMN_NAMES, COLUMNS, PAGE_SIZE, TITLE_PATTERNS, WORDS

# sitemap 索引下的叶子分片：前两个明文 xml，第三个 gzip 压缩（部分分片压缩后读取）
SHARD_FILES: list[str] = ["sitemap-1.xml", "sitemap-2.xml", "sitemap-3.xml.gz"]
SHARD_COUNT = 3
# 非法 lastmod：合并规则无法裁决，canonical 进待复核
CONFLICT_INVALID_LASTMOD = "2026-13-99"
# 跨分片冲突注入目标（栏目首条；三站栏目集合一致，路径都存在）
CONFLICT_TARGET_COLUMNS = ["zcwj", "czjs", "zdxm", "rsrm"]

def _default_fixture_base() -> Path:
    explicit = os.environ.get("SITEMAP_FIXTURE_DIR")
    if explicit:
        return Path(explicit)
    app_dir = Path("/app")
    if app_dir.is_dir():
        return app_dir / "sample_site"
    return Path.cwd() / "sample_site"


FIXTURE_BASE = _default_fixture_base()
LASTMOD_ANCHOR = date(2026, 10, 8)
TRACK_EVERY = 7   # 每 7 条加一次 utm/sessionid 追踪参数
DUP_EVERY = 9     # 每 9 条多写一个重复锚点（带 #fragment）


def fixture_root(site_code: str, base: Path | None = None) -> Path:
    root = Path(base) if base is not None else FIXTURE_BASE
    return root / site_code


def column_size(column_code: str) -> int:
    for code, _name, size in COLUMNS:
        if code == column_code:
            return size
    return 0


def page_count(column_code: str) -> int:
    size = column_size(column_code)
    return max(1, (size + PAGE_SIZE - 1) // PAGE_SIZE)


def item_path(column_code: str, item_no: int) -> str:
    return f"/xxgk/{column_code}/{column_code}-{item_no:03d}.html"


def item_title(column_code: str, item_no: int) -> str:
    patterns = TITLE_PATTERNS[column_code]
    words = WORDS[column_code]
    pattern = patterns[(item_no - 1) % len(patterns)]
    return pattern.format(w=words[(item_no - 1) % len(words)])


def item_lastmod(column_code: str, item_no: int) -> str:
    span = (item_no * 3 + len(column_code) * 5) % 40
    return (LASTMOD_ANCHOR - timedelta(days=span)).isoformat()


def item_page(column_code: str, item_no: int) -> int:
    return (item_no - 1) // PAGE_SIZE + 1


def catalog_rows(spec: dict) -> list[dict]:
    """该站 120 条公开目录条目（CatalogItem 的 A 集合来源）。"""
    rows: list[dict] = []
    for column_code, column_name, size in COLUMNS:
        for item_no in range(1, size + 1):
            path = item_path(column_code, item_no)
            rows.append(
                {
                    "column_code": column_code,
                    "column_name": column_name,
                    "entry_url": f"{spec['home_url']}{path}",
                    "canonical_url": f"{spec['home_url']}{path}",
                    "page_no": item_page(column_code, item_no),
                    "item_no": item_no,
                    "depth": 2,
                    "discovered_from": "home_link" if column_code in spec.get("no_rel_next", []) and item_page(column_code, item_no) > 1 else "rel_next",
                    "title": item_title(column_code, item_no),
                }
            )
    return rows


def sitemap_rows(spec: dict) -> list[dict]:
    """该站 sitemap.xml 的 loc 列表：目录条目去掉 missing，再加上 dead 死链。"""
    missing = {tuple(pair) for pair in spec.get("missing", [])}
    rows: list[dict] = []
    for column_code, _column_name, size in COLUMNS:
        for item_no in range(1, size + 1):
            if (column_code, item_no) in missing:
                continue
            path = item_path(column_code, item_no)
            rows.append(
                {
                    "loc": f"{spec['home_url']}{path}",
                    "canonical_url": f"{spec['home_url']}{path}",
                    "lastmod": item_lastmod(column_code, item_no),
                    "changefreq": "weekly",
                    "priority": 0.8,
                    "source_file": "sitemap.xml",
                }
            )
    for dead_path in spec.get("dead", []):
        rows.append(
            {
                "loc": f"{spec['home_url']}{dead_path}",
                "canonical_url": f"{spec['home_url']}{dead_path}",
                "lastmod": "2026-09-12",
                "changefreq": "monthly",
                "priority": 0.5,
                "source_file": "sitemap.xml",
            }
        )
    return rows


def sitemap_xml(rows: list[dict]) -> str:
    body = []
    for row in rows:
        body.append(
            "  <url>\n"
            f"    <loc>{row['loc']}</loc>\n"
            f"    <lastmod>{row['lastmod']}</lastmod>\n"
            f"    <changefreq>{row['changefreq']}</changefreq>\n"
            f"    <priority>{row['priority']}</priority>\n"
            "  </url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(body)
        + "\n</urlset>\n"
    )


def sitemap_index_xml(spec: dict, shard_files: list[str]) -> str:
    """sitemap 入口：一个 sitemapindex 指向同站多个分片（含 .xml.gz）。"""
    home = spec["home_url"].rstrip("/")
    body = [
        "  <sitemap>\n"
        f"    <loc>{home}/{name}</loc>\n"
        f"    <lastmod>{LASTMOD_ANCHOR.isoformat()}</lastmod>\n"
        "  </sitemap>"
        for name in shard_files
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(body)
        + "\n</sitemapindex>\n"
    )


def _conflict_plan(spec: dict, rows: list[dict]) -> dict[str, dict]:
    """每个站点 4 个确定性跨分片 lastmod 冲突（canonical -> 分片内重复副本）。

    - zcwj：重复副本 lastmod 更新（lastmod_latest 裁决）；
    - czjs：重复副本 lastmod 更旧（仍取最新的基础分片值，依据 lastmod_latest）；
    - zdxm：重复副本 lastmod 与基础值相同（并列，按 shard_order_min 裁决）；
    - rsrm：重复副本 lastmod 非法（无法裁决，进待复核）。
    所有重复副本都落在与基础条目不同的分片，且不引入新的 canonical（集合口径不变）。
    """
    row_index = {row["loc"]: position for position, row in enumerate(rows)}
    plans: dict[str, dict] = {}
    for column_code in CONFLICT_TARGET_COLUMNS:
        path = item_path(column_code, 1)
        loc = f"{spec['home_url']}{path}"
        position = row_index.get(loc)
        if position is None:
            continue
        base = rows[position]
        base_order = position % SHARD_COUNT  # 与 sitemap_shards 的切分逻辑保持一致
        dup_order = (base_order + 1) % SHARD_COUNT  # 必然跨分片
        base_date = base["lastmod"]
        if column_code == "zcwj":
            dup_lastmod = (date.fromisoformat(base_date) + timedelta(days=10)).isoformat()
        elif column_code == "czjs":
            dup_lastmod = (date.fromisoformat(base_date) - timedelta(days=10)).isoformat()
        elif column_code == "zdxm":
            dup_lastmod = base_date  # 并列 -> shard_order_min
        else:
            dup_lastmod = CONFLICT_INVALID_LASTMOD  # 非法 -> 待复核
        plans[base["canonical_url"]] = {
            "base": dict(base),
            "base_order": base_order,
            "dup": {
                "loc": base["loc"],
                "canonical_url": base["canonical_url"],
                "lastmod": dup_lastmod,
                "changefreq": base["changefreq"],
                "priority": base["priority"],
                "source_file": SHARD_FILES[dup_order],
            },
            "dup_order": dup_order,
        }
    return plans


def sitemap_shards(spec: dict, rows: list[dict] | None = None) -> list[dict]:
    """把该站 sitemap 全集切成 SHARD_COUNT 个分片，并注入跨分片 lastmod 冲突。

    返回 ``[{"file", "gzip", "rows"}]``；canonical 全集与 :func:`sitemap_rows` 完全一致，
    重复条目只是同一 canonical 在另一个分片再出现一次。
    """
    rows = rows if rows is not None else sitemap_rows(spec)
    buckets: list[list[dict]] = [[] for _ in range(SHARD_COUNT)]
    for index, row in enumerate(rows):
        order = index % SHARD_COUNT
        buckets[order].append({**row, "source_file": SHARD_FILES[order]})

    for plan in _conflict_plan(spec, rows).values():
        buckets[plan["dup_order"]].append(plan["dup"])

    return [
        {"file": SHARD_FILES[index], "gzip": SHARD_FILES[index].endswith(".gz"), "rows": shard_rows}
        for index, shard_rows in enumerate(buckets)
    ]


def _head(title: str, canonical: str | None) -> str:
    canonical_line = f'  <link rel="canonical" href="{canonical}" />\n' if canonical else ""
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"  <title>{title}</title>\n"
        f"{canonical_line}"
        "</head>\n<body>\n"
    )


def _listing_html(spec: dict, column_code: str, page_no: int) -> str:
    column_name = COLUMN_NAMES[column_code]
    home = spec["home_url"].rstrip("/")
    size = column_size(column_code)
    total_pages = page_count(column_code)
    start = (page_no - 1) * PAGE_SIZE + 1
    end = min(size, page_no * PAGE_SIZE)
    no_rel_next = column_code in spec.get("no_rel_next", [])

    nav = [f'  <a href="{home}/index.html">首页</a>', f'  <a href="{home}/xxgk/{column_code}/index.html">{column_name}</a>']
    pager = []
    if page_no > 1:
        pager.append(f'  <a class="pager" href="index.html">上一页</a>')
    if page_no < total_pages:
        pager.append(f'  <a class="pager" href="index_{page_no + 1}.html">下一页</a>')

    items = []
    for item_no in range(start, end + 1):
        href = f"{column_code}-{item_no:03d}.html"
        title = item_title(column_code, item_no)
        if item_no % TRACK_EVERY == 0:
            href = f"{href}?utm_source=wechat&utm_medium=social&sessionid=s-{item_no:03d}"
        items.append(
            f'    <li><a href="{href}">{title}</a>'
            f"<time>{item_lastmod(column_code, item_no)}</time></li>"
        )
        if item_no % DUP_EVERY == 0:
            items.append(
                f'    <li class="mirror"><a href="{column_code}-{item_no:03d}.html#top">{title}（移动端镜像）</a></li>'
            )

    head_extra = ""
    if page_no < total_pages and not no_rel_next:
        head_extra += f'  <link rel="next" href="index_{page_no + 1}.html" />\n'
    if page_no > 1:
        head_extra += f'  <link rel="prev" href="index_{page_no - 1}.html" />\n'
    canonical = f"{home}/xxgk/{column_code}/" if page_no == 1 else f"{home}/xxgk/{column_code}/index_{page_no}.html"

    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        '  <meta charset="utf-8" />\n'
        f"  <title>{column_name} 第 {page_no} 页 · {spec['name']}</title>\n"
        f'  <link rel="canonical" href="{canonical}" />\n'
        f"{head_extra}"
        "</head>\n<body>\n"
        '<nav class="crumbs">\n' + "\n".join(nav) + "\n</nav>\n"
        f"<h1>{column_name}</h1>\n"
        '<ul class="list">\n' + "\n".join(items) + "\n</ul>\n"
        '<div class="pager">\n' + "\n".join(pager) + "\n</div>\n"
        f'<footer>{spec["dept"]}</footer>\n'
        "</body>\n</html>\n"
    )


def _detail_html(spec: dict, column_code: str, item_no: int) -> str:
    column_name = COLUMN_NAMES[column_code]
    home = spec["home_url"].rstrip("/")
    path = item_path(column_code, item_no)
    self_url = f"{home}{path}"
    mismatch = (spec.get("canonical_mismatch") or {}).get(path)
    canonical = f"{home}{mismatch}" if mismatch else self_url
    title = item_title(column_code, item_no)
    return (
        _head(f"{title} · {column_name} · {spec['name']}", canonical)
        + f'<nav class="crumbs"><a href="{home}/index.html">首页</a> / '
        f'<a href="{home}/xxgk/{column_code}/index.html">{column_name}</a></nav>\n'
        f"<article>\n  <h1>{title}</h1>\n"
        f"  <p class=\"meta\">索引号：{column_code.upper()}-2026-{item_no:03d}　成文日期：{item_lastmod(column_code, item_no)}</p>\n"
        f"  <div class=\"body\">\n    <p>{title}已经市政府同意，现予公开。本文件自发布之日起施行。</p>\n"
        f"    <p>责任部门：{spec['dept']}　公开范围：主动公开</p>\n  </div>\n"
        "</article>\n"
        f"</body>\n</html>\n"
    )


def _home_html(spec: dict) -> str:
    home = spec["home_url"].rstrip("/")
    links = [
        f'    <li><a href="{home}/xxgk/{code}/index.html">{name}</a>'
        f"<span>{size} 条</span></li>"
        for code, name, size in COLUMNS
    ]
    links.append('    <li><a href="/xxgk/ndbg/">政府信息公开年度报告</a><span>专栏目</span></li>')
    extras = [
        '  <a href="https://www.hezhou.gov.cn/">河州市人民政府门户</a>',
        '  <a href="mailto:zwgk@hezhou.gov.cn">意见征集邮箱</a>',
        '  <a href="/xxgk/zcwj/index_9.html">历史归档索引</a>',
    ]
    return (
        _head(f"{spec['name']} · 政务公开目录", f"{home}/")
        + '<nav class="crumbs">\n' + "\n".join(extras) + "\n</nav>\n"
        + f"<h1>{spec['name']}</h1>\n"
        + f'<p class="meta">{spec["dept"]}　{spec["region"]}</p>\n'
        + '<ul class="columns">\n' + "\n".join(links) + "\n</ul>\n"
        + f"<footer>本站共 {sum(size for _c, _n, size in COLUMNS)} 条主动公开信息，sitemap.xml 由编辑部季度导出。</footer>\n"
        + "</body>\n</html>\n"
    )


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def build_site(spec: dict, root: Path) -> dict:
    """把一个站点写成磁盘静态站，返回写盘统计（幂等：同样入参写出同样字节）。"""
    root.mkdir(parents=True, exist_ok=True)
    home = spec["home_url"].rstrip("/")
    written = 0

    _write(root, "/index.html", _home_html(spec))
    written += 1
    _write(root, "/robots.txt", f"User-agent: *\nAllow: /\nSitemap: {home}/sitemap.xml\n")
    written += 1

    for column_code, _column_name, size in COLUMNS:
        pages = page_count(column_code)
        for page_no in range(1, pages + 1):
            relative = f"/xxgk/{column_code}/index.html" if page_no == 1 else f"/xxgk/{column_code}/index_{page_no}.html"
            _write(root, relative, _listing_html(spec, column_code, page_no))
            written += 1
        for item_no in range(1, size + 1):
            _write(root, item_path(column_code, item_no), _detail_html(spec, column_code, item_no))
            written += 1

    # sitemap 入口是索引，下面挂同站多个分片（最后一个 gzip）；同 canonical 跨分片重复
    rows = sitemap_rows(spec)
    shards = sitemap_shards(spec, rows)
    _write(root, "/sitemap.xml", sitemap_index_xml(spec, [shard["file"] for shard in shards]))
    written += 1
    for shard in shards:
        payload = sitemap_xml(shard["rows"]).encode("utf-8")
        if shard["gzip"]:
            payload = gzip.compress(payload, mtime=0)
        target = root / shard["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written += 1

    status_map: dict[str, int] = {}
    for dead_path in spec.get("dead", []):
        status_map[dead_path] = 404
    _write(root, "/status.json", json.dumps(status_map, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write(
        root,
        "/redirects.json",
        json.dumps(dict(sorted((spec.get("redirects") or {}).items())), ensure_ascii=False, indent=2) + "\n",
    )
    written += 2
    return {"root": str(root), "files": written, "sitemap": len(rows), "shards": len(shards)}


def ensure_site_fixture(spec: dict, base: Path | None = None) -> dict:
    """fixture 缺失或 sitemap 索引/分片不完整时重建；返回写盘统计。"""
    root = fixture_root(spec["code"], base)
    expected = root / "sitemap.xml"
    if expected.is_file() and (root / "index.html").is_file():
        # 只做完整性抽检，避免每次巡检都重写 400 个文件
        missing = [code for code, _n, _s in COLUMNS if not (root / "xxgk" / code / "index.html").is_file()]
        missing_shards = [name for name in SHARD_FILES if not (root / name).is_file()]
        if not missing and not missing_shards:
            return {"root": str(root), "files": 0, "sitemap": 0, "reused": True}
    return {**build_site(spec, root), "reused": False}


def build_all(sites: list[dict], base: Path | None = None) -> list[dict]:
    return [build_site(spec, fixture_root(spec["code"], base)) for spec in sites]
