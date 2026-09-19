"""companies admin API 测试：增删改查 + slug/ats_type 校验 + 连带删除。"""

from __future__ import annotations

import pytest


def _payload(slug="acme", ats_type="greenhouse", **overrides) -> dict:
    body = {
        "slug": slug,
        "name": "Acme",
        "ats_type": ats_type,
        "feed_url": "https://boards-api.greenhouse.io/v1/board/acme/jobs",
    }
    body.update(overrides)
    return body


def test_create_and_list(session, client):
    resp = client.post("/api/companies", json=_payload())
    assert resp.status_code == 200
    company_id = resp.json()["data"]["id"]
    assert resp.json()["data"]["is_active"] is True

    resp = client.get("/api/companies")
    assert resp.json()["data"]["total"] == 1
    item = resp.json()["data"]["items"][0]
    assert item["slug"] == "acme" and item["job_count"] == 0


def test_create_duplicate_slug_409(session, client):
    client.post("/api/companies", json=_payload())
    resp = client.post("/api/companies", json=_payload())
    assert resp.status_code == 409


def test_create_unregistered_ats_type_400(session, client):
    resp = client.post("/api/companies", json=_payload(ats_type="unknown_ats"))
    assert resp.status_code == 400
    assert "未注册" in resp.json()["detail"]


def test_update_company(session, client):
    company_id = client.post("/api/companies", json=_payload()).json()["data"]["id"]
    # 启停 + 改名
    resp = client.put(f"/api/companies/{company_id}", json=_payload(name="Acme2", is_active=False))
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is False
    assert resp.json()["data"]["name"] == "Acme2"
    # is_active 过滤
    assert client.get("/api/companies", params={"is_active": True}).json()["data"]["total"] == 0
    assert client.get("/api/companies", params={"is_active": False}).json()["data"]["total"] == 1


def test_update_slug_conflict_409(session, client):
    client.post("/api/companies", json=_payload(slug="a"))
    cid_b = client.post("/api/companies", json=_payload(slug="b")).json()["data"]["id"]
    resp = client.put(f"/api/companies/{cid_b}", json=_payload(slug="a"))
    assert resp.status_code == 409


def test_delete_guarded_and_force(session, client):
    from app.models import Company, Job, MatchScore, Resume

    company_id = client.post("/api/companies", json=_payload()).json()["data"]["id"]
    job = Job(company_id=company_id, external_id="J1", title="A", apply_url="https://x.com/1",
              source="greenhouse", status="active")
    session.add(job)
    session.commit()
    resume = Resume(user_id=1, profile={"skills": ["python"]})
    session.add(resume)
    session.commit()
    session.add(MatchScore(resume_id=resume.id, job_id=job.id, rule_score=0.8, final_score=0.8))
    session.commit()

    # 默认拒绝
    resp = client.delete(f"/api/companies/{company_id}")
    assert resp.status_code == 409
    # force 连带删除：职位 + match_scores + 公司
    resp = client.delete(f"/api/companies/{company_id}", params={"force": True})
    assert resp.status_code == 200
    assert resp.json()["data"]["jobs_deleted"] == 1
    assert session.get(Company, company_id) is None
    assert session.query(Job).count() == 0
    assert session.query(MatchScore).count() == 0


def test_delete_missing_404(session, client):
    assert client.delete("/api/companies/999").status_code == 404
