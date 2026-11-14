"""sitemap 读取器单测：索引分层展开、gzip 分片、同站校验、环路/深度/解析失败、确定性。"""
from __future__ import annotations

import gzip
from pathlib import Path

from src.services import sitemap_reader
from src.services.crawl_engine import file_path_for

HOME = "https://mini.gov.cn"


def _write(root: Path, url: str, content: bytes) -> None:
    target = root / file_path_for(url).lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _urlset(urls: list[str]) -> bytes:
    body = "".join(
        f"  <url><loc>{loc}</loc><lastmod>2026-10-01</lastmod></url>\n" for loc in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + body
        + "</urlset>\n"
    ).encode()


def _index(locs: list[str]) -> bytes:
    body = "".join(
        "  <sitemap>"
        f"<loc>{loc}</loc><lastmod>2026-10-08</lastmod>"
        "</sitemap>\n"
        for loc in locs
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + body
        + "</sitemapindex>\n"
    ).encode()


def test_flatten_index_with_plain_and_gzip_shards(tmp_path):
    loader = sitemap_reader.SitemapFixture(tmp_path)
    index_url = f"{HOME}/sitemap.xml"
    shard_urls = [f"{HOME}/sitemap-{n}.xml" for n in (1, 2)] + [f"{HOME}/sitemap-3.xml.gz"]
    _write(tmp_path, index_url, _index(shard_urls))
    _write(tmp_path, shard_urls[0], _urlset([f"{HOME}/a.html"]))
    _write(tmp_path, shard_urls[1], _urlset([f"{HOME}/b.html"]))
    _write(tmp_path, shard_urls[2], gzip.compress(_urlset([f"{HOME}/c.html"]), mtime=0))

    plan = sitemap_reader.flatten_index(loader, HOME, index_url)
    assert plan["skipped"] == []
    assert [shard["shard_order"] for shard in plan["shards"]] == [1, 2, 3]
    assert plan["shards"][0]["compressed"] is False
    assert plan["shards"][2]["compressed"] is True
    # 每个分片都有内容指纹
    assert all(shard["content_sha256"] for shard in plan["shards"])

    expanded = sitemap_reader.expand_all(loader, HOME, index_url)
    assert expanded["errors"] == []
    canonicals = sorted(record["canonical_url"] for record in expanded["records"])
    assert canonicals == [f"{HOME}/a.html", f"{HOME}/b.html", f"{HOME}/c.html"]


def test_nested_index_expands_layer_by_layer(tmp_path):
    loader = sitemap_reader.SitemapFixture(tmp_path)
    root_index = f"{HOME}/sitemap.xml"
    nested = f"{HOME}/sub/index.xml"
    leaf = f"{HOME}/sub/leaf.xml"
    _write(tmp_path, root_index, _index([nested]))
    _write(tmp_path, nested, _index([leaf]))
    _write(tmp_path, leaf, _urlset([f"{HOME}/n.html"]))

    plan = sitemap_reader.flatten_index(loader, HOME, root_index)
    assert len(plan["shards"]) == 1
    assert plan["shards"][0]["loc"] == leaf
    assert plan["shards"][0]["depth"] == 2


def test_cross_site_shard_is_skipped_not_followed(tmp_path):
    loader = sitemap_reader.SitemapFixture(tmp_path)
    index_url = f"{HOME}/sitemap.xml"
    evil = "https://other.gov.cn/evil.xml"
    _write(tmp_path, index_url, _index([evil, f"{HOME}/ok.xml"]))
    _write(tmp_path, f"{HOME}/ok.xml", _urlset([f"{HOME}/ok.html"]))

    plan = sitemap_reader.flatten_index(loader, HOME, index_url)
    assert len(plan["shards"]) == 1
    assert plan["skipped"] == [{"url": evil, "reason": "cross_site"}]


def test_index_loop_is_guarded(tmp_path):
    loader = sitemap_reader.SitemapFixture(tmp_path)
    first = f"{HOME}/a.xml"
    second = f"{HOME}/b.xml"
    _write(tmp_path, first, _index([second]))
    _write(tmp_path, second, _index([first]))  # 指回 first -> 环路

    plan = sitemap_reader.flatten_index(loader, HOME, first)
    assert plan["shards"] == []
    assert any(item["reason"] == "index_loop" for item in plan["skipped"])


def test_missing_and_broken_shard_return_error_not_raise(tmp_path):
    loader = sitemap_reader.SitemapFixture(tmp_path)
    index_url = f"{HOME}/sitemap.xml"
    # 索引里挂一个磁盘不存在的 gzip 分片 + 一个内容损坏的分片 + 一个正常分片
    missing = f"{HOME}/missing.xml.gz"
    broken = f"{HOME}/broken.xml"
    ok = f"{HOME}/ok.xml"
    _write(tmp_path, index_url, _index([missing, broken, ok]))
    _write(tmp_path, broken, b"<?xml version='1.0'?><urlset><url><loc>")  # 非法 XML
    _write(tmp_path, ok, _urlset([f"{HOME}/ok.html"]))

    plan = sitemap_reader.flatten_index(loader, HOME, index_url)
    # 缺失分片在展开阶段进 skipped；损坏分片同站可达，登记为可续跑的叶子检查点
    assert any(item["reason"] == "fixture_missing" and item["url"] == missing for item in plan["skipped"])
    by_file = {shard["source_file"]: shard for shard in plan["shards"]}
    assert set(by_file) == {"broken.xml", "ok.xml"}
    assert by_file["broken.xml"].get("parse_error")
    broken_out = sitemap_reader.read_shard(loader, by_file["broken.xml"])
    ok_out = sitemap_reader.read_shard(loader, by_file["ok.xml"])
    assert broken_out["error"] and "XML" in broken_out["error"]
    assert ok_out["error"] is None and len(ok_out["records"]) == 1

    # 直接对不存在的分片调用 read_shard 也返回 error 而不是抛异常
    ghost = {"loc": missing, "source_file": "missing.xml.gz", "shard_order": 9}
    assert sitemap_reader.read_shard(loader, ghost)["error"] == "fixture_missing"


def test_deterministic_shard_order_and_fingerprint(tmp_path):
    loader = sitemap_reader.SitemapFixture(tmp_path)
    index_url = f"{HOME}/sitemap.xml"
    shard_urls = [f"{HOME}/s-{n}.xml" for n in (1, 2)]
    _write(tmp_path, index_url, _index(shard_urls))
    payload = _urlset([f"{HOME}/x.html"])
    _write(tmp_path, shard_urls[0], payload)
    _write(tmp_path, shard_urls[1], payload)

    first = sitemap_reader.flatten_index(loader, HOME, index_url)
    second = sitemap_reader.flatten_index(loader, HOME, index_url)
    assert [s["loc"] for s in first["shards"]] == [s["loc"] for s in second["shards"]]
    assert [s["content_sha256"] for s in first["shards"]] == [
        s["content_sha256"] for s in second["shards"]
    ]
