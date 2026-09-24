"""上线合规资产契约（§14 ① 用户协议/隐私政策、② 投递授权与告知文案）。

后端：政策版本/生效日、投递告知要点、`consent_version` 校验与同意留痕、简历原件删除联动；
前端：协议/隐私政策正文（中英对照，单一事实源 `app/legal/legal-text.json`）——强制披露三节
中英齐备、中英小节与段落一一对应、英文不得残留中文（漏译最常见的形态）、无占位词、
效力声明与 `authoritative_lang` 一致，以及**版本号与后端一致**（跨端漂移会让"用户同意了哪一版"
无法核验，而 `next build` 不会报错）。

前端无自动化测试框架，沿用 `test_pwa_assets.py` / `test_web_i18n.py` 的
"后端 pytest 校验前端静态资产内容契约"口径；`scripts/export_legal_text.py` 用同一套规则出送审稿。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.main import app, get_session
from app.models import Resume

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
LEGAL_TEXT = FRONTEND / "app" / "legal" / "legal-text.json"
DOCS = ("terms", "privacy")
CJK = re.compile(r"[\u4e00-\u9fff]")
PLACEHOLDERS = ("TODO", "TBD", "待补", "待定", "XXX", "Lorem ipsum")
# 强制三节各落在哪份文档（§14 ① 括号内要求：授权在用户协议，删除与最小必要在隐私政策）
REQUIRED_SECTION_HOME = {"authorization": "terms", "minimal": "privacy", "deletion": "privacy"}


@pytest.fixture(autouse=True)
def _seal_llm(monkeypatch):
    """测试密封：本机 .env 常配了 llm_api_key，强制清空让简历解析走规则路径
    （与 test_resume_parse.py 同口径）——否则上传用例会真的外呼 LLM，既慢又不确定。"""
    monkeypatch.setattr(config.settings, "llm_api_key", "")


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _legal_text() -> dict:
    return json.loads(LEGAL_TEXT.read_text(encoding="utf-8"))


def _section_text(section: dict) -> str:
    """小节全文（含标题），用于漏译/占位词扫描。"""
    parts = [section["h"]]
    for block in section["body"]:
        parts.extend(block["ul"] if "ul" in block else [block["p"]])
    return "".join(parts)


# ---------- 后端：政策版本 + 告知文案 ----------


def test_policies_payload_covers_required_documents_and_sections():
    from app.services.legal import (
        APPLY_NOTICE,
        POLICY_EFFECTIVE_DATE,
        POLICY_VERSION,
        REQUIRED_SECTION_IDS,
        policies_payload,
    )

    data = policies_payload()
    assert data["policy_version"] == POLICY_VERSION and POLICY_VERSION
    assert data["effective_date"] == POLICY_EFFECTIVE_DATE
    keys = [d["key"] for d in data["documents"]]
    assert keys == ["terms", "privacy"]
    for doc in data["documents"]:
        assert doc["version"] == POLICY_VERSION and doc["effective_date"] == POLICY_EFFECTIVE_DATE
        assert doc["path"].startswith("/legal/")
    # 强制披露三节（§14 ① 括号内要求）：中文节名给人看，id 给前端正文与校验脚本用
    assert data["required_sections"] == ["授权范围", "个人信息删除", "最小必要"]
    assert data["required_section_ids"] == REQUIRED_SECTION_IDS
    # 告知文案与 PRD 对齐：不代提交 / 跳第三方 / 最小必要 / 可撤回
    assert len(APPLY_NOTICE["points"]) >= 4
    joined = "".join(APPLY_NOTICE["points"]) + APPLY_NOTICE["consent_label"]
    for keyword in ("第三方", "不会代填密码", "最小必要", "撤回"):
        assert keyword in joined, f"告知文案缺少关键口径：{keyword}"


def test_legal_policies_api(client):
    resp = client.get("/api/legal/policies")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["policy_version"] and len(data["documents"]) == 2
    assert data["apply_notice"]["consent_label"]


# ---------- 前端：/legal/* 正文（中英对照单一事实源） ----------


def test_legal_text_zh_en_sections_align():
    """中英逐条对照：小节 id/顺序一致、段落块类型与列表条数一一对应。

    漏译最典型的形态是"英文少了一段/少了一条"，页面照样渲染、`next build` 照样过，
    只能靠结构对齐来兜。
    """
    text = _legal_text()
    for doc in DOCS:
        zh, en = text[doc]["zh"], text[doc]["en"]
        assert zh and en, f"{doc}：中英正文不得为空"
        assert [s["id"] for s in zh] == [s["id"] for s in en], f"{doc}：中英小节 id/顺序不一致"
        for zh_sec, en_sec in zip(zh, en):
            assert len(zh_sec["body"]) == len(en_sec["body"]), f"{doc}/{zh_sec['id']}：中英段落数不一致"
            for zh_block, en_block in zip(zh_sec["body"], en_sec["body"]):
                assert ("ul" in zh_block) == ("ul" in en_block), f"{doc}/{zh_sec['id']}：中英块类型不一致"
                assert zh_block.keys() == en_block.keys(), f"{doc}/{zh_sec['id']}：中英块字段不一致"
                if "ul" in zh_block:
                    assert len(zh_block["ul"]) == len(en_block["ul"]), f"{doc}/{zh_sec['id']}：中英列表条数不一致"


def test_legal_text_covers_required_sections_in_both_langs():
    """强制披露三节（§14 ①）：id 来自后端单一事实源，中英两套都要有且落在指定文档。"""
    from app.services.legal import REQUIRED_SECTION_IDS

    text = _legal_text()
    ids = {doc: {s["id"] for s in text[doc]["zh"]} for doc in DOCS}
    en_ids = {doc: {s["id"] for s in text[doc]["en"]} for doc in DOCS}
    for rid in REQUIRED_SECTION_IDS:
        home = REQUIRED_SECTION_HOME[rid]
        assert rid in ids[home], f"强制披露小节 {rid} 不在 {home} 中"
        assert rid in en_ids[home], f"强制披露小节 {rid} 缺英文版"
    # §14 ① 原文里点名的两条口径要在正文里真的写出来（此前只靠页面注释兜，这里改断正文节名）
    privacy_zh = "".join(_section_text(s) for s in text["privacy"]["zh"])
    privacy_en = "".join(_section_text(s) for s in text["privacy"]["en"])
    assert "最小必要" in privacy_zh and "个人信息的删除与撤回" in privacy_zh
    assert "原件文件同时删除" in privacy_zh  # 与 DELETE /api/resumes 的实际口径一致
    assert "Data Minimization" in privacy_en and "Deletion of Personal Information" in privacy_en
    assert "original uploaded resume file" in privacy_en


def test_legal_english_text_is_translated_not_pasted():
    """英文版：不得残留中文（粘贴未翻译段的最常见形态），且每节不是一句话占位。"""
    text = _legal_text()
    for doc in DOCS:
        for section in text[doc]["en"]:
            body = _section_text(section)
            hit = CJK.search(body)
            assert not hit, f"{doc}/{section['id']}：英文小节残留中文「{hit and hit.group(0)}」"
            assert len(body) >= 40, f"{doc}/{section['id']}：英文小节过短（{len(body)} 字），疑似未翻译"


def test_legal_text_has_no_placeholder_markers():
    text = _legal_text()
    for doc in DOCS:
        for lang in ("zh", "en"):
            for section in text[doc][lang]:
                body = _section_text(section)
                for marker in PLACEHOLDERS:
                    assert marker not in body, f"{doc}/{section['id']}[{lang}]：残留占位词「{marker}」"


def test_legal_disclaimer_matches_authoritative_lang():
    """效力声明必须与 meta.authoritative_lang 同口径（声明"中文为准"就不能反过来）。"""
    text = _legal_text()
    assert text["meta"]["authoritative_lang"] == "zh"
    assert "解释依据" in text["disclaimer"]["zh"]
    assert "prevail" in text["disclaimer"]["en"]


def test_legal_pages_render_shared_text_module():
    """两页渲染的是同一份 JSON（不再各自内联文案），语言生效声明也从 JSON 出。"""
    shell = _read("app/legal/legal-doc.js")
    assert 'from "./legal-text.json"' in shell
    assert "export function LegalSections" in shell and "legalDisclaimer" in shell
    # 语言码映射：cookie 是 zh-CN/en，正文 JSON 是 zh/en——映射错了中文页会渲染成空白
    # （`doc["zh-CN"]` 取不到 key 不报错，只是页面没正文），故把映射本身钉在契约里
    assert 'normalizeLang(lang) === "en" ? "en" : "zh"' in shell
    for page in ("terms", "privacy"):
        source = _read(f"app/legal/{page}/page.js")
        assert "LegalSections" in source and f'docKey="{page}"' in source
        assert "<h2>" not in source  # 正文已抽走，页面不再内联小节标题
    # 旧的"英文版与法务终稿属待办"占位文案必须消失（英文版已落地）
    assert "英文版本与法务终稿属待办" not in _read("app/i18n.js")


def test_frontend_legal_version_matches_backend():
    """版本号/生效日跨端一致：前端硬编码必须等于后端常量（否则同意版本对不上）。"""
    from app.services.legal import POLICY_EFFECTIVE_DATE, POLICY_VERSION

    shell = _read("app/legal/legal-doc.js")
    assert f'POLICY_VERSION = "{POLICY_VERSION}"' in shell
    assert f'POLICY_EFFECTIVE_DATE = "{POLICY_EFFECTIVE_DATE}"' in shell


def test_home_links_to_legal_pages():
    page = _read("app/page.js")
    assert 'href="/legal/terms"' in page and 'href="/legal/privacy"' in page


def test_home_apply_flow_shows_notice_before_consent():
    """投递必须先展示告知并勾选（不再是"点击即视为授权"），且把政策版本原样回传。"""
    page = _read("app/page.js")
    assert "FALLBACK_NOTICE" in page  # 告知文案取不到时也有兜底，不静默投递
    assert '"/api/legal/policies"' in page
    assert "consent_version: notice?.version" in page
    assert "authorized: agreed" in page
    assert "点击即视为用户确认授权投递" not in page  # 旧的隐式授权口径已移除
    assert 'type="checkbox"' in page and "disabled={!agreed}" in page  # 未勾选不能提交


# ---------- 投递授权留痕（§14 ②） ----------


def _mk_job(session, eid="t-legal-apply"):
    from app.models import Job

    job = Job(
        external_id=eid,
        title="后端工程师",
        city="杭州市",
        skills=["python"],
        apply_url="https://example.com/apply",
        source="test",
        status="active",
    )
    session.add(job)
    session.commit()
    return job


def test_apply_rejects_mismatched_consent_version(session, client):
    from app.services.legal import POLICY_VERSION

    job = _mk_job(session)
    resp = client.post(
        "/api/applications",
        json={"user_id": 1, "job_id": job.id, "authorized": True, "consent_version": "0.9"},
    )
    assert resp.status_code == 400
    assert POLICY_VERSION in resp.json()["detail"]  # 提示当前版本，便于前端刷新重新确认

    ok = client.post(
        "/api/applications",
        json={"user_id": 1, "job_id": job.id, "authorized": True, "consent_version": POLICY_VERSION},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["consent_version"] == POLICY_VERSION


def test_apply_records_consent_trail(session, client):
    """授权留痕：authorized / authorized_at / consent_version 三者齐备，并在看板接口透出。"""
    from app.models import Application
    from app.services.legal import POLICY_VERSION

    job = _mk_job(session, "t-legal-trail")
    # 未带版本（服务端调用/老客户端）→ 按当前版本补记，不留空
    resp = client.post("/api/applications", json={"user_id": 1, "job_id": job.id, "authorized": True})
    assert resp.status_code == 200
    row = session.get(Application, resp.json()["data"]["id"])
    assert row.authorized is True
    assert row.authorized_at is not None
    assert row.consent_version == POLICY_VERSION

    items = client.get("/api/applications", params={"user_id": 1}).json()["data"]["items"]
    assert items[0]["authorized"] is True
    assert items[0]["authorized_at"] and items[0]["consent_version"] == POLICY_VERSION


# ---------- 个人信息删除：删库同时删盘 ----------


@pytest.fixture()
def client(session, tmp_path, monkeypatch):
    """本轮专用 client：uploads_dir 指向 tmp_path，验证原件删除。"""
    monkeypatch.setattr(config.settings, "uploads_dir", str(tmp_path))

    def override_get_session():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


def test_delete_resume_removes_uploaded_file(session, client, tmp_path):
    resp = client.post(
        "/api/resumes",
        files={"file": ("resume.txt", "8 年 Python 后端经验".encode("utf-8"), "text/plain")},
    )
    rid = resp.json()["data"]["id"]
    stored = Path(session.get(Resume, rid).file_path)
    assert stored.exists()

    body = client.delete(f"/api/resumes/{rid}").json()["data"]
    assert body["deleted"] is True and body["file_removed"] is True
    assert not stored.exists()  # 个人信息删除：盘上原件同步清理


def test_delete_resume_refuses_path_outside_uploads_dir(session, client, tmp_path):
    """库内 file_path 指向 uploads_dir 之外（历史脏数据）时拒绝删除，不误删他人文件。"""
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("must not be deleted", encoding="utf-8")
    resume = Resume(user_id=1, raw_text="x", profile={}, file_path=str(outside))
    session.add(resume)
    session.commit()

    body = client.delete(f"/api/resumes/{resume.id}").json()["data"]
    assert body["file_removed"] is False
    assert outside.exists()  # 目录外文件不受影响


def test_delete_resume_without_file_is_ok(session, client):
    rid = client.post("/api/resumes", data={"raw_text": "纯文本简历", "user_id": "3"}).json()["data"]["id"]
    body = client.delete(f"/api/resumes/{rid}").json()["data"]
    assert body["file_removed"] is False  # 无原件文件（raw_text 上传）时照常删库行