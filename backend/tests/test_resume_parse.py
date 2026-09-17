"""简历解析管道测试（P2 §6）：规则抽取、防幻觉校验、整管道、上传 API。

全部离线：LLM 路径用 monkeypatch 模拟，不触网、不需要 api key。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_session
from app.pipelines.parse import parse_resume_text
from app.pipelines.rules import extract_profile_rules, validate_profile
from app.pipelines.text_extract import UnsupportedFile, extract_text


RESUME_ZH = """张三的简历
联系方式：zhangsan@example.com

教育背景：某大学 计算机科学与技术 本科

工作经历：
2018-2026 某公司 后端工程师，8年开发经验
技术栈：Python、FastAPI、MySQL、K8s

求职意向：高级后端工程师
期望城市：杭州、上海
期望薪资：30K-45K
"""

RESUME_EN = "Senior Backend Engineer with 6 years experience. Skills: Python, Go.\n" * 3


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """测试密封：默认清掉 llm_api_key（本机 .env 可能配了 key），
    需要 LLM 路径的用例自行 monkeypatch 覆盖。"""
    from app.core import config

    monkeypatch.setattr(config.settings, "llm_api_key", "")


# ---------- 规则抽取 ----------


def test_extract_rules_chinese():
    p = extract_profile_rules(RESUME_ZH)
    assert p["experience_years"] == 8
    assert p["edu_degree"] == "bachelor"
    assert p["salary_min"] == 30 and p["salary_max"] == 45
    assert p["cities"] == ["杭州", "上海"]
    assert p["target_role"] == "高级后端工程师"
    assert p["lang"] == "zh"


def test_extract_rules_english_lang():
    p = extract_profile_rules(RESUME_EN)
    assert p["lang"] == "en"
    assert p["experience_years"] == 6


def test_extract_rules_degree_priority():
    # 同时出现硕士/博士时取最高学历
    p = extract_profile_rules("博士学历，此前硕士毕业于某校")
    assert p["edu_degree"] == "phd"


def test_extract_rules_salary_single_value():
    p = extract_profile_rules("期望薪资 35K")
    assert p["salary_min"] == 35 and p["salary_max"] == 35


# ---------- 防幻觉校验 ----------


def test_validate_llm_missing_fields_filled():
    rule = extract_profile_rules(RESUME_ZH)
    notes: list[str] = []
    merged = validate_profile({"skills": ["python"]}, rule, notes)
    assert merged["experience_years"] == 8
    assert merged["salary_min"] == 30
    assert "experience_years_from_rules" in notes


def test_validate_llm_year_conflict_uses_rule():
    rule = extract_profile_rules(RESUME_ZH)
    notes: list[str] = []
    merged = validate_profile({"experience_years": 2, "skills": []}, rule, notes)
    assert merged["experience_years"] == 8  # 偏差 >3 年 → 以规则为准
    assert any("experience_years_conflict" in n for n in notes)


def test_validate_llm_bad_salary_uses_rule():
    rule = extract_profile_rules(RESUME_ZH)
    notes: list[str] = []
    merged = validate_profile({"salary_min": 80, "salary_max": 20}, rule, notes)
    assert merged["salary_min"] == 30 and merged["salary_max"] == 45


# ---------- 整管道 ----------


def test_parse_resume_rules_fallback(session):
    # 无 llm_api_key → 规则兜底；skills 走字典扫描并归一（K8s→kubernetes）
    profile = parse_resume_text(RESUME_ZH, session)
    assert profile["source"] == "rules"
    assert "python" in profile["skills"]
    assert "kubernetes" in profile["skills"]
    assert profile["experience_years"] == 8


def test_parse_resume_llm_path(session, monkeypatch):
    from app.core import config
    from app.pipelines import parse as parse_mod

    monkeypatch.setattr(config.settings, "llm_api_key", "fake-key")

    def fake_llm(text):
        return {"skills": ["Python", "Kafka"], "experience_years": 8, "target_role": "后端专家"}

    monkeypatch.setattr(parse_mod, "llm_extract_profile", fake_llm)
    profile = parse_resume_text(RESUME_ZH, session)
    assert profile["source"] == "llm"
    # LLM 技能 ∪ 词典扫描（"K8s"→kubernetes、"后端"→backend、"MySQL"→mysql）归一
    assert profile["skills"] == ["backend", "kafka", "kubernetes", "mysql", "python"]
    assert profile["target_role"] == "后端专家"


def test_parse_resume_llm_failure_falls_back(session, monkeypatch):
    from app.core import config
    from app.pipelines import parse as parse_mod

    monkeypatch.setattr(config.settings, "llm_api_key", "fake-key")

    def boom(text):
        raise RuntimeError("network down")

    monkeypatch.setattr(parse_mod, "llm_extract_profile", boom)
    profile = parse_resume_text(RESUME_ZH, session)
    assert profile["source"] == "rules"
    assert any("llm_failed_fallback_rules" in n for n in profile.get("notes", []))


def test_parse_resume_empty_raises(session):
    with pytest.raises(ValueError):
        parse_resume_text("   \n  ", session)


# ---------- 文本抽取 ----------


def _make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj ".encode() + body + b" endobj\n"
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    return out


def test_extract_text_pdf():
    text = extract_text("resume.pdf", _make_pdf("Python Backend 5 years"))
    assert "Python Backend 5 years" in text


def test_extract_text_rejects_unsupported():
    with pytest.raises(UnsupportedFile):
        extract_text("resume.docx", b"x")


def test_extract_text_rejects_oversize():
    with pytest.raises(UnsupportedFile):
        extract_text("big.pdf", b"x" * (5 * 1024 * 1024 + 1))


# ---------- 上传 API ----------


@pytest.fixture()
def client(session, tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "uploads_dir", str(tmp_path))

    def override_get_session():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def test_upload_txt_and_get(session, client):
    resp = client.post(
        "/api/resumes",
        files={"file": ("resume.txt", RESUME_ZH.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["source"] == "rules"
    assert body["profile"]["experience_years"] == 8

    got = client.get(f"/api/resumes/{body['id']}")
    assert got.status_code == 200
    assert got.json()["data"]["profile"]["salary_max"] == 45


def test_upload_raw_text_and_update_profile(session, client):
    resp = client.post("/api/resumes", data={"raw_text": RESUME_ZH, "user_id": "7"})
    assert resp.status_code == 200
    rid = resp.json()["data"]["id"]

    # 人工修正：改目标薪资与城市，source 置 manual
    upd = client.put(
        f"/api/resumes/{rid}/profile",
        json={"profile": {"salary_min": 35, "salary_max": 55, "cities": ["上海"], "foo": "bar"}},
    )
    assert upd.status_code == 200
    profile = upd.json()["data"]["profile"]
    assert profile["salary_min"] == 35 and profile["salary_max"] == 55
    assert profile["cities"] == ["上海"]
    assert profile["source"] == "manual"
    assert "foo" not in profile  # 白名单外字段不落库


def test_upload_rejects_bad_type(session, client):
    resp = client.post("/api/resumes", files={"file": ("r.docx", b"x", "application/octet-stream")})
    assert resp.status_code == 400


def test_upload_requires_input(session, client):
    resp = client.post("/api/resumes")
    assert resp.status_code == 400
