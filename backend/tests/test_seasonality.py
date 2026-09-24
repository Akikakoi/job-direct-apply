"""招聘季适配 + 多语言标签测试（§12.6 P5 ⑤）：月份/季度分布、旺淡季判定、样本下限、接口。"""

from __future__ import annotations

from datetime import date

from app.models import Job
from app.services.i18n import DEFAULT_LANG, SUPPORTED_LANGS, labels, month_label, resolve_lang
from app.services.seasonality import MIN_DATED_JOBS, _season_of, seasonality_report


def _job(source="netease", publish=None, external_id="J"):
    return Job(
        external_id=external_id,
        title="职位",
        city="杭州",
        apply_url=f"https://x.com/{external_id}",
        source=source,
        status="active",
        publish_date=publish,
    )


def _seed_by_month(session, counts: dict[int, int], year: int = 2025, source="netease"):
    """按 {月份: 条数} 播种职位，external_id 自增避免唯一约束撞车。"""
    n = 0
    for month, count in counts.items():
        for _ in range(count):
            n += 1
            session.add(_job(source=source, publish=date(year, month, 10), external_id=f"S{n}"))
    session.commit()


def test_labels_and_lang_resolution():
    assert SUPPORTED_LANGS == ("zh-CN", "en")
    assert labels("en")["region"]["overseas"] == "Overseas"
    assert month_label("en", 3) == "Mar"
    assert month_label("zh-CN", 3) == "3 月"
    # 未知语言回退默认（展示层不该因语言串崩掉）
    assert resolve_lang("fr") == DEFAULT_LANG
    assert resolve_lang(None) == DEFAULT_LANG
    assert labels("fr") is labels(DEFAULT_LANG)


def test_season_of_thresholds():
    assert _season_of(24, 2.0) == "peak"       # 24 >= 2.0 * 1.2
    assert _season_of(0, 2.0) == "off"         # 0 <= 2.0 * 0.8
    assert _season_of(2, 2.0) == "shoulder"
    assert _season_of(5, 0.0) == "shoulder"    # 月均 0 时不误判


def test_publish_date_only_never_falls_back_to_created_at(session):
    """created_at 有值但 publish_date 为空 → 不计入季节分布（避免把采集月份当招聘旺季）。"""
    session.add(_job(publish=None, external_id="NODATE"))
    session.commit()
    report = seasonality_report(session, now=date(2026, 9, 24))
    assert report["scope"]["with_publish_date"] == 0
    assert report["scope"]["unknown_publish_date"] == 1
    assert all(row["count"] == 0 for row in report["by_month"])


def test_insufficient_sample_gives_no_season_conclusion(session):
    _seed_by_month(session, {3: 2, 9: 1})  # 3 条 < MIN_DATED_JOBS
    report = seasonality_report(session, now=date(2026, 3, 15))
    assert MIN_DATED_JOBS == 12
    assert report["season_summary"]["enough_sample"] is False
    assert report["season_summary"]["peak_months"] == []
    assert all(row["season"] is None for row in report["by_month"])
    assert report["current"]["season"] is None
    assert report["current"]["season_label"] is None
    assert "样本不足" in report["current"]["advice"]
    assert any("不足以判定旺季" in n for n in report["notes"])


def test_month_and_quarter_distribution(session):
    # 3 月 24 条、7 月 12 条 → 月均 3.0：3 月 peak（≥3.6）、7 月 peak（≥3.6）
    _seed_by_month(session, {3: 24, 7: 12})
    report = seasonality_report(session, now=date(2026, 3, 15))

    assert report["scope"]["with_publish_date"] == 36
    assert report["scope"]["total_jobs"] == 36
    assert report["scope"]["years_covered"] == 1
    assert report["season_summary"]["monthly_avg"] == 3.0
    assert report["season_summary"]["peak_months"] == [3, 7]
    march = report["by_month"][2]
    assert march["month_label"] == "3 月"
    assert march["count"] == 24 and march["season"] == "peak"
    assert march["share"] == 0.6667
    # 零值月份补齐 + 淡季
    assert report["by_month"][0]["count"] == 0 and report["by_month"][0]["season"] == "off"
    # 季度聚合：Q1 = 24，Q3 = 12
    assert report["by_quarter"][0]["quarter_label"] == "一季度"
    assert report["by_quarter"][0]["count"] == 24
    assert report["by_quarter"][2]["count"] == 12


def test_current_season_and_lang(session):
    _seed_by_month(session, {3: 24, 7: 12})

    cn = seasonality_report(session, lang="zh-CN", now=date(2026, 3, 15))
    assert cn["current"]["month"] == 3
    assert cn["current"]["season"] == "peak"
    assert cn["current"]["season_label"] == "旺季"
    assert cn["current"]["quarter_label"] == "一季度"
    assert "招聘旺季" in cn["current"]["advice"]

    en = seasonality_report(session, lang="en", now=date(2026, 7, 1))
    assert en["lang"] == "en"
    assert en["current"]["month_label"] == "Jul"
    assert en["current"]["season_label"] == "peak"
    assert en["current"]["quarter_label"] == "Q3"
    assert "Peak hiring season" in en["current"]["advice"]
    assert en["labels"]["region"]["cn"] == "China"
    assert any("publish_date" in n for n in en["notes"])

    # 非法语言在服务层回退（API 层才 422）
    assert seasonality_report(session, lang="fr")["lang"] == "zh-CN"


def test_years_note_when_single_year(session):
    _seed_by_month(session, {3: 24}, year=2025)
    assert any("个年份" in n for n in seasonality_report(session, now=date(2026, 3, 15))["notes"])

    # 跨两个年份 → 年份提示消失，且月份跨年叠加
    _seed_by_month(session, {9: 12}, year=2024)
    report = seasonality_report(session, now=date(2026, 3, 15))
    assert report["scope"]["years_covered"] == 2
    assert report["scope"]["min_year"] == 2024 and report["scope"]["max_year"] == 2025
    assert not any("个年份" in n for n in report["notes"])
    assert report["by_month"][8]["count"] == 12


def test_region_filter_and_label(session):
    _seed_by_month(session, {3: 24}, source="netease")
    _seed_by_month(session, {3: 12}, source="greenhouse")

    cn = seasonality_report(session, region="cn", now=date(2026, 3, 15))
    assert cn["scope"]["region_label"] == "国内"
    assert cn["scope"]["with_publish_date"] == 24

    overseas = seasonality_report(session, region="overseas", lang="en", now=date(2026, 3, 15))
    assert overseas["scope"]["region_label"] == "Overseas"
    assert overseas["scope"]["with_publish_date"] == 12

    # 海外只有 12 条 → 刚好达下限，仍给结论
    assert overseas["season_summary"]["enough_sample"] is True


def test_seasonality_api(session, client):
    _seed_by_month(session, {3: 24, 7: 12})

    resp = client.get("/api/insights/seasonality")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["lang"] == "zh-CN"
    assert data["scope"]["with_publish_date"] == 36
    assert len(data["by_month"]) == 12 and len(data["by_quarter"]) == 4

    en = client.get("/api/insights/seasonality", params={"region": "cn", "lang": "en"})
    assert en.status_code == 200
    assert en.json()["data"]["labels"]["region"]["cn"] == "China"

    assert client.get("/api/insights/seasonality", params={"region": "moon"}).status_code == 422
    assert client.get("/api/insights/seasonality", params={"lang": "fr"}).status_code == 422
    assert client.get("/api/insights/seasonality", params={"min_sample": 0}).status_code == 422