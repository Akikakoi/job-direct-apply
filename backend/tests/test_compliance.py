"""采集合规审计表用例（§14 ③）。

重点不在"字段有没有"，而在两条容易悄无声息失效的约束：
① **覆盖**：seed 名单里每家公司都必须有审计行——新增公司漏审计，等于在没有 robots/terms
   结论的情况下开采集（§5.2 合规前置），靠用例兜住而不是靠人记；
② **枚举**：robots/terms/decision 取值固定，防止"结论"退化成自由文本（无法比对与统计）。
"""

from __future__ import annotations

from app.services.compliance import (
    AUDIT_ROWS,
    ROBOTS_CONCLUSIONS,
    TERMS_CONCLUSIONS,
    audit_report,
    markdown_table,
    validate_audit_rows,
)
from app.services.seed import COMPANY_SEED


def test_every_seed_company_has_audit_row():
    """覆盖硬约束：seed 内每家公司都要有审计行（漏一家就漏一次合规前置）。"""
    seeded = {item["slug"] for item in COMPANY_SEED}
    audited = {row["slug"] for row in AUDIT_ROWS}
    assert seeded - audited == set(), f"以下 seed 公司缺审计行：{sorted(seeded - audited)}"


def test_audit_rows_are_structurally_valid():
    assert validate_audit_rows() == []
    for row in AUDIT_ROWS:
        assert row["robots"] in ROBOTS_CONCLUSIONS
        assert row["terms"] in TERMS_CONCLUSIONS
        assert row["reviewed_at"]  # 复核日期是结论有效性的前提


def test_validate_flags_bad_enum_and_missing_field():
    bad = [{"slug": "x", "robots": "whatever", "terms": "public_api_ok", "decision": "collect"}]
    problems = validate_audit_rows(bad)
    assert any("robots 取值非法" in p for p in problems)
    assert any("缺少 robots_note" in p for p in problems)


def test_dji_is_dormant_with_bypass_reason():
    """大疆休眠的理由必须写清"不绕反爬"（§5.2），不能只标个 dormant 了事。"""
    dji = next(r for r in AUDIT_ROWS if r["slug"] == "dji")
    assert dji["decision"] == "dormant"
    assert dji["robots"] == "halted" and dji["terms"] == "halted"
    assert "绕过反爬" in dji["terms_note"] and "Moka" in dji["terms_note"]


def test_netease_records_missing_robots_fallback():
    """网易 robots 无真实文件属已知情况，结论与低频策略必须留证。"""
    netease = next(r for r in AUDIT_ROWS if r["slug"] == "netease")
    assert netease["robots"] == "no_robots_fallback"
    assert "720" in netease["robots_note"]  # 低频承诺写进证据


def test_report_joins_db_companies_and_flags_missing(session):
    from app.models import Company

    session.add_all(
        [
            Company(slug="netease", name="网易", ats_type="netease", is_active=True),
            Company(slug="brand-new", name="新公司", ats_type="greenhouse", is_active=True),
        ]
    )
    session.commit()

    report = audit_report(session)
    assert report["coverage"]["companies"] == 2
    assert report["coverage"]["audited"] == 1
    assert report["coverage"]["ratio"] == 0.5
    # 新增公司未补审计行 → 必须被单列，而不是静默当作"没问题"
    assert report["coverage"]["missing_slugs"] == ["brand-new"]
    assert report["unreviewed"] == ["brand-new"]
    assert report["collecting"] == ["netease"]
    rows = {r["slug"]: r for r in report["rows"]}
    assert rows["netease"]["audited"] is True and rows["netease"]["terms_note"]
    assert rows["brand-new"]["audited"] is False


def test_markdown_table_renders_all_rows(session):
    """文档 §14 的表格由脚本从「库内公司 × 审计行」渲染，含公司名与结论（可直接粘贴）。"""
    from app.models import Company

    session.add_all([Company(slug=i["slug"], name=i["name"], ats_type=i["ats_type"]) for i in COMPANY_SEED])
    session.commit()

    table = markdown_table(audit_report(session)["rows"])
    lines = table.strip().splitlines()
    assert lines[0].startswith("| 公司 |")
    assert len(lines) == len(COMPANY_SEED) + 2  # 表头 + 分隔行 + 每公司一行
    assert "| 网易（netease） |" in table
    assert "| 大疆 DJI（dji） |" in table and "dormant" in table


def test_compliance_audit_api(client, session):
    from app.models import Company

    session.add(Company(slug="stripe", name="Stripe", ats_type="greenhouse", is_active=True))
    session.commit()

    resp = client.get("/api/compliance/audit")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["coverage"]["audited"] == 1 and data["unreviewed"] == []
    assert data["rows"][0]["robots_note"] and data["rows"][0]["reviewed_at"]
    assert data["notes"]