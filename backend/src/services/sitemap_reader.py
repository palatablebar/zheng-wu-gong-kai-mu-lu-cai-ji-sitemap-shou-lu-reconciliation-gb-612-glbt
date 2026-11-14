"""sitemap 索引/分片读取器（离线 fixture，确定性、可恢复）。

站点 sitemap 入口现在是一个 ``sitemapindex``，下面挂多个分片，部分分片以 gzip
压缩（``.xml.gz``），同一规范化 URL 会在不同分片重复出现。本模块只负责
「字节 -> 结构化记录」，不做合并（见 :mod:`sitemap_merge`）、不写库，
因此可以被快照服务按分片逐个调用，配合分片级检查点实现断点续跑：

- ``flatten_index``：逐层展开索引（嵌套索引也支持），同站校验、深度上限、
  环路保护；跨站分片进 ``skipped``，不静默放行；
- ``read_shard``：读单个分片（自动 gunzip），失败返回 ``error`` 而不是抛异常，
  由调用方把分片标记为 failed、批次保持 expanding 等待续跑；
- 每个分片带内容 sha256，快照指纹由此算出，保证「同一批快照」可校验。
"""
from __future__ import annotations

import gzip
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

from src.services.crawl_engine import file_path_for
from src.services.url_normalize import normalize_url, same_site

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_INDEX_DEPTH = 4
GZIP_SUFFIXES = (".gz", ".gzip")


class SitemapFixture:
    """一个站点 fixture 的 sitemap 字节读取层（HTTP 语义的本地替身）。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _target(self, url: str) -> Path:
        return self.root / file_path_for(url).lstrip("/")

    def exists(self, url: str) -> bool:
        return self._target(url).is_file()

    def read_bytes(self, url: str) -> bytes | None:
        target = self._target(url)
        if not target.is_file():
            return None
        try:
            return target.read_bytes()
        except OSError:
            return None


def is_gzip_url(url: str) -> bool:
    return str(url).lower().endswith(GZIP_SUFFIXES)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(node: ET.Element, name: str) -> str | None:
    for child in node:
        if _strip_ns(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def parse_sitemap(blob: bytes, *, source_url: str = "") -> dict:
    """解析一段 sitemap 字节（自动识别 gzip）。

    返回 ``{"kind": "index"|"urlset"|None, "shards": [], "urls": [], "error": None|str}``。
    解析失败不抛异常，返回 kind=None + error，交由调用方决定重试/续跑。
    """
    try:
        if is_gzip_url(source_url) or blob[:2] == b"\x1f\x8b":
            blob = gzip.decompress(blob)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        return {"kind": None, "shards": [], "urls": [], "error": f"gzip 解压失败：{exc}"}

    try:
        root = ET.fromstring(blob)
    except ET.ParseError as exc:
        return {"kind": None, "shards": [], "urls": [], "error": f"XML 解析失败：{exc}"}

    kind = _strip_ns(root.tag)
    if kind == "sitemapindex":
        shards = []
        for node in root:
            if _strip_ns(node.tag) != "sitemap":
                continue
            loc = _child_text(node, "loc")
            if loc:
                shards.append({"loc": loc, "lastmod": _child_text(node, "lastmod")})
        return {"kind": "index", "shards": shards, "urls": [], "error": None}

    if kind == "urlset":
        urls = []
        for node in root:
            if _strip_ns(node.tag) != "url":
                continue
            loc = _child_text(node, "loc")
            if not loc:
                continue
            priority_raw = _child_text(node, "priority")
            try:
                priority = float(priority_raw) if priority_raw is not None else None
            except ValueError:
                priority = None
            urls.append(
                {
                    "loc": loc,
                    "lastmod": _child_text(node, "lastmod"),
                    "changefreq": _child_text(node, "changefreq"),
                    "priority": priority,
                }
            )
        return {"kind": "urlset", "shards": [], "urls": urls, "error": None}

    return {"kind": None, "shards": [], "urls": [], "error": f"根元素不是 sitemapindex/urlset：{kind}"}


def flatten_index(loader: SitemapFixture, home_url: str, index_url: str) -> dict:
    """逐层展开索引，返回有序叶子分片清单与跳过项。

    - 只接受与站点首页同站的分片 loc，跨站/非 http 进 skipped；
    - 嵌套索引递归展开，深度超过 ``MAX_INDEX_DEPTH`` 跳过；
    - visited 集合防止索引互相引用形成环路；
    - shard_order 按发现顺序全局编号，是合并规则里并列裁决的确定性依据。
    """
    home = normalize_url(home_url)
    shards: list[dict] = []
    skipped: list[dict] = []
    visited: set[str] = set()
    order = 0

    def walk(url: str, depth: int) -> None:
        nonlocal order
        current = normalize_url(url)
        if not current:
            skipped.append({"url": url, "reason": "non_http"})
            return
        if current in visited:
            skipped.append({"url": current, "reason": "index_loop"})
            return
        visited.add(current)
        if depth > MAX_INDEX_DEPTH:
            skipped.append({"url": current, "reason": "depth_exceeded"})
            return
        if not same_site(current, home):
            skipped.append({"url": current, "reason": "cross_site"})
            return
        blob = loader.read_bytes(current)
        if blob is None:
            skipped.append({"url": current, "reason": "fixture_missing"})
            return
        parsed = parse_sitemap(blob, source_url=current)
        if parsed["error"] or parsed["kind"] is None:
            # 同站、文件可达但内容损坏：登记为叶子分片检查点（读取阶段会置 failed），
            # 而不是静默跳过——这样损坏分片可在断点续跑时重试，而不是从计划里消失。
            order += 1
            shards.append(
                {
                    "loc": current,
                    "source_file": file_path_for(current).lstrip("/"),
                    "shard_order": order,
                    "compressed": is_gzip_url(current),
                    "depth": depth,
                    "content_sha256": hashlib.sha256(blob).hexdigest(),
                    "parse_error": parsed["error"] or "unknown_parse_error",
                }
            )
            skipped.append({"url": current, "reason": "parse_error", "detail": parsed["error"]})
            return
        if parsed["kind"] == "index":
            for item in parsed["shards"]:
                walk(item["loc"], depth + 1)
            return
        # 叶子分片（urlset）
        order += 1
        shards.append(
            {
                "loc": current,
                "source_file": file_path_for(current).lstrip("/"),
                "shard_order": order,
                "compressed": is_gzip_url(current),
                "depth": depth,
                "content_sha256": hashlib.sha256(blob).hexdigest(),
            }
        )

    walk(index_url, 0)
    return {"index_url": normalize_url(index_url), "shards": shards, "skipped": skipped}


def read_shard(loader: SitemapFixture, shard: dict) -> dict:
    """读一个叶子分片，输出带分片来源标记的 url 记录（失败不抛异常）。"""
    loc = str(shard["loc"])
    blob = loader.read_bytes(loc)
    if blob is None:
        return {
            "shard_order": shard.get("shard_order"),
            "source_file": shard.get("source_file"),
            "records": [],
            "error": "fixture_missing",
            "content_sha256": None,
        }
    digest = hashlib.sha256(blob).hexdigest()
    parsed = parse_sitemap(blob, source_url=loc)
    if parsed["error"] or parsed["kind"] is None:
        return {
            "shard_order": shard.get("shard_order"),
            "source_file": shard.get("source_file"),
            "records": [],
            "error": parsed["error"] or "unknown_parse_error",
            "content_sha256": digest,
        }
    records = [
        {
            "loc": item["loc"],
            "canonical_url": normalize_url(item["loc"]),
            "lastmod": item.get("lastmod"),
            "changefreq": item.get("changefreq"),
            "priority": item.get("priority"),
            "source_file": shard.get("source_file") or file_path_for(loc).lstrip("/"),
            "shard_order": shard.get("shard_order"),
        }
        for item in parsed["urls"]
    ]
    return {
        "shard_order": shard.get("shard_order"),
        "source_file": shard.get("source_file"),
        "records": records,
        "error": None,
        "content_sha256": digest,
    }


def expand_all(loader: SitemapFixture, home_url: str, index_url: str) -> dict:
    """一次性展开索引并读完所有分片（测试/无断点场景用；服务端走分片续跑）。"""
    plan = flatten_index(loader, home_url, index_url)
    shard_results = []
    all_records: list[dict] = []
    errors: list[dict] = []
    for shard in plan["shards"]:
        result = read_shard(loader, shard)
        shard_results.append({**shard, **result})
        if result["error"]:
            errors.append({"source_file": shard["source_file"], "error": result["error"]})
        else:
            all_records.extend(result["records"])
    return {
        "index_url": plan["index_url"],
        "shards": shard_results,
        "records": all_records,
        "skipped": plan["skipped"],
        "errors": errors,
    }
