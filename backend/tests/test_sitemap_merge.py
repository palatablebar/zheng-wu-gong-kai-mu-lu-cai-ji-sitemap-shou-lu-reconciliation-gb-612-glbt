"""sitemap 分片合并规则单测：lastmod 裁决优先级、并列兜底、待复核、来源保留、确定性。"""
from __future__ import annotations

from src.services import sitemap_merge

C = "https://mini.gov.cn/a.html"


def _source(loc: str, lastmod, order: int, source_file: str | None = None) -> dict:
    return {
        "loc": loc,
        "canonical_url": C,
        "lastmod": lastmod,
        "source_file": source_file or f"sitemap-{order}.xml",
        "shard_order": order,
        "changefreq": "weekly",
        "priority": 0.8,
    }


def test_single_source_needs_no_decision():
    result = sitemap_merge.merge_records([_source(C, "2026-10-01", 1)])
    conclusion = result["merged"][C]
    assert conclusion["decision_basis"] == sitemap_merge.BASIS_SINGLE_SOURCE
    assert conclusion["duplicate"] is False
    assert conclusion["needs_review"] is False
    assert conclusion["source_count"] == 1
    assert result["stats"]["conflict_total"] == 0


def test_latest_lastmod_wins_in_either_direction():
    # 副本更新 -> 取副本；副本更旧 -> 取基础值，依据都是 lastmod_latest
    newer = sitemap_merge.merge_records(
        [_source(C, "2026-09-01", 1), _source(C, "2026-10-01", 2)]
    )["merged"][C]
    older = sitemap_merge.merge_records(
        [_source(C, "2026-10-01", 1), _source(C, "2026-09-01", 2)]
    )["merged"][C]
    assert newer["decision_basis"] == sitemap_merge.BASIS_LASTMOD_LATEST
    assert newer["lastmod"] == "2026-10-01"
    assert newer["source_file"] == "sitemap-2.xml"
    assert older["decision_basis"] == sitemap_merge.BASIS_LASTMOD_LATEST
    assert older["lastmod"] == "2026-10-01"
    assert older["source_file"] == "sitemap-1.xml"


def test_tie_on_lastmod_uses_min_shard_order():
    result = sitemap_merge.merge_records(
        [_source(C, "2026-10-01", 3), _source(C, "2026-10-01", 1), _source(C, "2026-10-01", 2)]
    )
    conclusion = result["merged"][C]
    assert conclusion["decision_basis"] == sitemap_merge.BASIS_SHARD_ORDER_MIN
    assert conclusion["source_file"] == "sitemap-1.xml"


def test_tie_on_both_uses_loc_lex_min():
    records = [
        _source("https://mini.gov.cn/a.html", "2026-10-01", 1),
        _source("https://mini.gov.cn/a.html", "2026-10-01", 1),
    ]
    # 同分片同 lastmod：loc 字典序兜底（这里 loc 相同，仍应稳定取第一条且不报错）
    conclusion = sitemap_merge.merge_records(records)["merged"][C]
    assert conclusion["decision_basis"] == sitemap_merge.BASIS_LOC_LEX_MIN


def test_unparsable_lastmod_goes_to_review_but_keeps_temp_value():
    result = sitemap_merge.merge_records(
        [
            _source(C, "2026-10-01", 1),
            _source(C, "2026-13-99", 2),  # 非法日期
        ]
    )
    conclusion = result["merged"][C]
    assert conclusion["needs_review"] is True
    assert conclusion["decision_basis"] == sitemap_merge.BASIS_REVIEW_PENDING
    assert "lastmod_unparsable" in conclusion["review_reasons"]
    # 仍给确定性临时采用值（合法来源里的最新值），巡检可继续
    assert conclusion["lastmod"] == "2026-10-01"
    assert result["stats"]["needs_review_total"] == 1


def test_all_unparsable_still_deterministic_and_reviewable():
    result = sitemap_merge.merge_records(
        [_source(C, None, 2), _source(C, "not-a-date", 1)]
    )
    conclusion = result["merged"][C]
    assert conclusion["needs_review"] is True
    assert set(conclusion["review_reasons"]) == {"lastmod_missing", "lastmod_unparsable"}
    # 无合法来源时按分片序号兜底
    assert conclusion["source_file"] == "sitemap-1.xml"
    again = sitemap_merge.merge_records(
        [_source(C, None, 2), _source(C, "not-a-date", 1)]
    )
    assert again["merged"][C] == conclusion


def test_all_sources_are_preserved():
    result = sitemap_merge.merge_records(
        [_source(C, "2026-09-01", 1), _source(C, "2026-10-01", 2), _source(C, "2026-08-01", 3)]
    )
    sources = result["merged"][C]["sources"]
    assert [s["shard_order"] for s in sources] == [1, 2, 3]
    assert [s["lastmod"] for s in sources] == ["2026-09-01", "2026-10-01", "2026-08-01"]
    assert result["merged"][C]["lastmod_values"] == ["2026-08-01", "2026-09-01", "2026-10-01"]


def test_merge_is_deterministic_and_counts_conflicts():
    records = [
        _source(C, "2026-09-01", 1),
        _source(C, "2026-10-01", 2),
        {
            "loc": "https://mini.gov.cn/b.html",
            "canonical_url": "https://mini.gov.cn/b.html",
            "lastmod": "2026-10-01",
            "source_file": "sitemap-1.xml",
            "shard_order": 1,
        },
    ]
    first = sitemap_merge.merge_records(records)
    second = sitemap_merge.merge_records(list(reversed(records)))
    assert first == second  # 输入顺序不影响结论
    assert first["stats"]["canonical_total"] == 2
    assert first["stats"]["url_total"] == 3
    assert first["stats"]["conflict_total"] == 1
