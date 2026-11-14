"""sitemap 分片合并规则（纯函数、确定性）。

一个规范化 URL（canonical）会在多个分片里重复出现且 lastmod 不一致。本模块只做
「读出来的分片记录 -> 每个 canonical 一条合并结论」，不碰数据库、不碰磁盘，
读取（sitemap_reader）、合并（本模块）、巡检（crawl_run_service）因此可以针对
**同一批快照**分别测试。

合并规则（顺序即优先级，全部可解释、可回放）：
1. 先按 canonical 归组，同组每条来源都原样保留（source_file / loc / lastmod / 序号）；
2. lastmod 必须是 ``YYYY-MM-DD``（或带时间的 ISO 串），无法解析 / 缺失记为
   ``lastmod_unparsable``，该 canonical 进入**待复核**，不参与自动裁决；
3. 全部来源 lastmod 都合法时，取**最新的 lastmod**（``lastmod_latest``）；
4. 最新 lastmod 并列时，取分片序号最小（``shard_order_min``）；序号也相同再按
   loc 字典序最小（``loc_lex_min``）——保证任何输入下结论唯一、可复现；
5. 只有一条来源且合法 -> ``single_source``，无需裁决。

待复核不阻断巡检：系统会给一个临时采用值（规则 4 的确定性兜底），巡检照常跑，
但结论带 ``needs_review=True``，页面与导出都能看到，等人工 PATCH 裁决。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

# 合并依据（decision_basis）取值：前端 config.js / 文档需同步
BASIS_SINGLE_SOURCE = "single_source"
BASIS_LASTMOD_LATEST = "lastmod_latest"
BASIS_SHARD_ORDER_MIN = "shard_order_min"
BASIS_LOC_LEX_MIN = "loc_lex_min"
BASIS_REVIEW_PENDING = "review_pending"

# 待复核原因
REVIEW_LASTMOD_UNPARSABLE = "lastmod_unparsable"
REVIEW_LASTMOD_MISSING = "lastmod_missing"

VALID_BASIS = (
    BASIS_SINGLE_SOURCE,
    BASIS_LASTMOD_LATEST,
    BASIS_SHARD_ORDER_MIN,
    BASIS_LOC_LEX_MIN,
    BASIS_REVIEW_PENDING,
)


def parse_lastmod(value: Any) -> date | None:
    """解析 sitemap 的 lastmod。合法返回 date，否则 None（含缺失/非法）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # W3C sitemap 允许 date 或带时间的 ISO 8601；统一截到日期部分再试
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _source_key(source: dict) -> tuple:
    """确定性兜底排序键：分片序号 -> loc 字典序 -> source_file。"""
    return (
        int(source.get("shard_order") if source.get("shard_order") is not None else 0),
        str(source.get("loc") or ""),
        str(source.get("source_file") or ""),
    )


def merge_records(records: list[dict]) -> dict:
    """把同一批分片读出的 url 记录合并成一个 canonical -> 结论的索引。

    入参每条记录需含：loc、canonical_url（缺省用 loc）、lastmod、source_file、
    shard_order（分片在索引中的序号）、可选 changefreq/priority。
    返回 ``{"merged": {canonical: conclusion}, "stats": {...}}``。
    """
    groups: dict[str, list[dict]] = {}
    for raw in records:
        canonical = str(raw.get("canonical_url") or raw.get("loc") or "").strip()
        if not canonical:
            continue
        groups.setdefault(canonical, []).append(raw)

    merged: dict[str, dict] = {}
    stats = {
        "url_total": len(records),
        "canonical_total": len(groups),
        "conflict_total": 0,
        "needs_review_total": 0,
        "by_basis": {basis: 0 for basis in VALID_BASIS},
    }

    for canonical, sources_raw in groups.items():
        sources = sorted(sources_raw, key=_source_key)
        source_view = [
            {
                "loc": str(item.get("loc") or canonical),
                "lastmod": item.get("lastmod"),
                "source_file": str(item.get("source_file") or "sitemap.xml"),
                "shard_order": int(item.get("shard_order") or 0),
                "lastmod_valid": parse_lastmod(item.get("lastmod")) is not None,
            }
            for item in sources
        ]

        parsed: list[tuple[date, dict]] = []
        invalid: list[dict] = []
        for item in sources:
            parsed_date = parse_lastmod(item.get("lastmod"))
            if parsed_date is None:
                invalid.append(item)
            else:
                parsed.append((parsed_date, item))

        is_duplicate = len(sources) > 1
        review_reasons: list[str] = []
        if invalid:
            review_reasons = [
                REVIEW_LASTMOD_MISSING if not str(item.get("lastmod") or "").strip() else REVIEW_LASTMOD_UNPARSABLE
                for item in invalid
            ]
            # 去重保序
            review_reasons = list(dict.fromkeys(review_reasons))

        needs_review = bool(review_reasons)

        if not needs_review and len(parsed) == 1:
            chosen = parsed[0][1]
            basis = BASIS_SINGLE_SOURCE
        elif not needs_review:
            # 全部合法且多于一条：最新 lastmod
            latest = max(d for d, _item in parsed)
            winners = [item for d, item in parsed if d == latest]
            if len(winners) == 1:
                chosen = winners[0]
                basis = BASIS_LASTMOD_LATEST
            else:
                # 最新并列：分片序号最小
                min_order = min(int(item.get("shard_order") or 0) for item in winners)
                order_winners = [item for item in winners if int(item.get("shard_order") or 0) == min_order]
                if len(order_winners) == 1:
                    chosen = order_winners[0]
                    basis = BASIS_SHARD_ORDER_MIN
                else:
                    chosen = sorted(order_winners, key=lambda item: str(item.get("loc") or ""))[0]
                    basis = BASIS_LOC_LEX_MIN
        else:
            # 待复核：仍给确定性临时采用值，优先在合法来源里按规则 3/4 选；
            # 一个合法来源都没有时按分片序号/loc 兜底，保证巡检可继续。
            basis = BASIS_REVIEW_PENDING
            if parsed:
                latest = max(d for d, _item in parsed)
                winners = [item for d, item in parsed if d == latest]
                min_order = min(int(item.get("shard_order") or 0) for item in winners)
                order_winners = [item for item in winners if int(item.get("shard_order") or 0) == min_order]
                chosen = sorted(order_winners, key=lambda item: str(item.get("loc") or ""))[0]
            else:
                chosen = sorted(invalid, key=_source_key)[0]

        if is_duplicate:
            stats["conflict_total"] += 1
        if needs_review:
            stats["needs_review_total"] += 1
        stats["by_basis"][basis] += 1

        lastmod_values = sorted({str(item.get("lastmod")) for item in sources if item.get("lastmod") is not None})
        merged[canonical] = {
            "canonical_url": canonical,
            "loc": str(chosen.get("loc") or canonical),
            "lastmod": chosen.get("lastmod"),
            "changefreq": chosen.get("changefreq"),
            "priority": chosen.get("priority"),
            "source_file": str(chosen.get("source_file") or "sitemap.xml"),
            "decision_basis": basis,
            "needs_review": needs_review,
            "review_reasons": review_reasons,
            "source_count": len(sources),
            "duplicate": is_duplicate,
            "lastmod_values": lastmod_values,
            "sources": source_view,
        }

    return {"merged": merged, "stats": stats}
