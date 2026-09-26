"""region 分区边界 + 方案 A 国内/海外交错展示测试（§4.2 / §12.7 #9、#10）。全部离线。"""

from __future__ import annotations

from app.models import Company, Job, Resume

PROFILE = {
    "skills": ["python", "fastapi"],
    "experience_years": 8,
    "target_role": "后端工程师",
    "cities": ["杭州"],
}


def _company(session, slug: str, ats_type: str = "greenhouse") -> Company:
    c = Company(slug=slug, name=slug, ats_type=ats_type)
    session.add(c)
    session.commit()
    return c


def _job(
    session,
    *,
    title: str,
    source: str | None,
    city: str = "杭州",
    skills=("python", "fastapi"),
    company_id: int | None = None,
) -> Job:
    job = Job(
        external_id=f"region-{title}-{source}",
        title=title,
        city=city,
        skills=list(skills),
        experience_min=5,
        apply_url=f"https://example.com/{title}",
        source=source,
        status="active",
        company_id=company_id,
    )
    session.add(job)
    session.commit()
    return job


def _resume(session) -> Resume:
    r = Resume(user_id=1, raw_text="x", profile=PROFILE, lang="zh")
    session.add(r)
    session.commit()
    return r


def _ids(body: dict) -> list[int]:
    return [it["job_id"] for it in body["items"]]


def _get(client, resume_id: int, **params) -> dict:
    resp = client.get("/api/recommend", params={"resume_id": resume_id, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------- 分区边界 ----------


def test_region_partition_and_null_source_goes_overseas(session, client):
    cn = _job(session, title="国内后端", source="netease")
    ov = _job(session, title="Overseas Backend", source="greenhouse", city="Remote")
    dirty = _job(session, title="脏数据源", source=None, city="新加坡")
    resume = _resume(session)

    cn_body = _get(client, resume.id, region="cn")
    assert _ids(cn_body) == [cn.id] and cn_body["total"] == 1

    # source 为 NULL 的脏数据归海外，规避 NOT IN 遇 NULL 全落空
    ov_body = _get(client, resume.id, region="overseas")
    assert set(_ids(ov_body)) == {ov.id, dirty.id} and ov_body["total"] == 2

    all_body = _get(client, resume.id, region="all")
    assert all_body["total"] == 3 and set(_ids(all_body)) == {cn.id, ov.id, dirty.id}


def test_region_invalid_422(session, client):
    resume = _resume(session)
    resp = client.get("/api/recommend", params={"resume_id": resume.id, "region": "mars"})
    assert resp.status_code == 422


# ---------- 方案 A：国内/海外交错 ----------


def test_all_region_interleaves_regions(session, client):
    """国内 3 条高分 + 海外 3 条低分：混排后海外不被整体挤出默认列表。"""
    cn = [_job(session, title="后端工程师", source="netease") for _ in range(3)]
    ov = [
        _job(session, title="后端工程师", source="greenhouse", city="Remote", skills=("python",))
        for _ in range(3)
    ]
    resume = _resume(session)

    body = _get(client, resume.id, limit=6, region="all")
    ids = _ids(body)
    assert len(ids) == 6
    cn_ids = {j.id for j in cn}
    flags = [i in cn_ids for i in ids]
    assert flags in ([True, False] * 3, [False, True] * 3)  # 严格 1:1 交错
    assert len(cn_ids & set(ids)) == 3  # 两区都有曝光，非纯分数序（纯分数序会全是国内）
    assert body["items"][0]["score"] >= body["items"][1]["score"]  # 首位来自分数更高的一路


def test_region_cn_keeps_pure_score_order(session, client):
    cn = [_job(session, title="后端工程师", source="netease") for _ in range(3)]
    _job(session, title="后端工程师", source="greenhouse", city="Remote")
    resume = _resume(session)

    body = _get(client, resume.id, region="cn")
    assert _ids(body) == [j.id for j in cn]


def test_all_region_pagination_is_stable(session, client):
    """分页窗口拼接 == 一次性取全（交错序与 offset/limit 无关）。"""
    for i in range(4):
        _job(session, title=f"国内后端工程师{i}", source="netease")
    for i in range(4):
        _job(session, title=f"后端工程师ov{i}", source="greenhouse", city="Remote", skills=("python",))
    resume = _resume(session)

    full = _ids(_get(client, resume.id, limit=8, region="all"))
    assert len(full) == 8 and len(set(full)) == 8

    paged: list[int] = []
    for offset in (0, 2, 4, 6):
        paged += _ids(_get(client, resume.id, limit=2, offset=offset, region="all"))
    assert paged == full


# ---------- 路内公司级轮转（方案 A 之后的实测缺口） ----------


def test_overseas_company_round_robin_breaks_monopoly(session, client):
    """海外单公司高分占满窗口：轮转后低分公司也能拿到靠前的曝光位。

    实测库内海外前 50 名 100% 来自 ashby 上的 3 家公司，stripe/equinox/nvidia
    一条都进不来——同为海外、同来源、同城市，只有公司不同时，纯分数序会把后进
    公司整体压在窗口尾部。
    """
    hot_company = _company(session, "hotco")
    cold_company = _company(session, "coldco")
    hot = [
        _job(
            session,
            title=f"Overseas Backend {i}",
            source="greenhouse",
            city="Remote",
            company_id=hot_company.id,
        )
        for i in range(3)
    ]
    cold = _job(
        session,
        title="Overseas Backend Cold",
        source="greenhouse",
        city="Remote",
        skills=(),
        company_id=cold_company.id,
    )
    resume = _resume(session)

    body = _get(client, resume.id, limit=4, region="overseas")
    ids = _ids(body)
    assert body["total"] == 4 and len(ids) == 4
    assert set(ids) == {j.id for j in hot} | {cold.id}
    # 纯分数序下 cold 垫底（第 4 位）；公司级轮转后它拿到第 2 位
    assert ids[1] == cold.id


def test_overseas_pagination_stable_across_companies(session, client):
    """多公司轮转下分页窗口拼接 == 一次性取全（轮转序与 offset/limit 无关）。"""
    for slug, city in (("alpha", "Remote"), ("beta", "Remote"), ("gamma", "Remote")):
        company = _company(session, slug)
        for i in range(3):
            _job(
                session,
                title=f"Backend {slug}{i}",
                source="greenhouse",
                city=city,
                skills=("python", "fastapi"),
                company_id=company.id,
            )
    resume = _resume(session)

    full = _ids(_get(client, resume.id, limit=9, region="overseas"))
    assert len(full) == 9 and len(set(full)) == 9

    paged: list[int] = []
    for offset in (0, 1, 2, 3, 4, 5, 6, 7, 8):
        paged += _ids(_get(client, resume.id, limit=1, offset=offset, region="overseas"))
    assert paged == full