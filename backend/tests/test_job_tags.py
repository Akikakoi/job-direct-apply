"""入库抽标签测试（§5.2 / §12.7 #7）：词典优先、LLM 兜底、cap 上限与失败不阻塞。

LLM 调用一律 monkeypatch，不触网；Redis 由 conftest 密封。
"""

from __future__ import annotations

from app.models import Company, Job
from app.pipelines import job_tags
from app.pipelines.job_tags import tag_from_dictionary, tag_jobs
from app.services.collect import build_alias_map, collect_company

ALIAS = {"k8s": "kubernetes", "kubernetes": "kubernetes", "python": "python", "go": "go"}


def make_company(session, slug="acme", ats_type="fake") -> Company:
    company = Company(slug=slug, name=slug.title(), ats_type=ats_type, fetch_policy={"interval_min": 60})
    session.add(company)
    session.commit()
    return company


def make_job(session, company, eid, title, description=None, skills=None) -> Job:
    job = Job(
        company_id=company.id,
        external_id=eid,
        title=title,
        description=description,
        skills=skills or [],
        apply_url=f"https://x.com/{eid}",
        source="fake",
        status="active",
    )
    session.add(job)
    session.commit()
    return job


def run_collect(session, monkeypatch, jobs):
    """走一遍 collect_company（用 test_collect 的 FakeAdapter，不触网）。"""
    from tests.test_collect import FakeAdapter

    company = make_company(session, slug="acme-collect")
    monkeypatch.setattr("app.services.collect.get_adapter", lambda t, client=None: FakeAdapter(jobs))
    return collect_company(session, company)


def test_tag_from_dictionary_hits_title_and_description():
    assert tag_from_dictionary("Senior Python Engineer", "K8s 集群", ALIAS) == ["kubernetes", "python"]
    assert tag_from_dictionary("Kubernetes 工程师", None, ALIAS) == ["kubernetes"]


def test_tag_jobs_dictionary_path_and_llm_skipped(session, monkeypatch):
    """词典能命中就不调 LLM；命不中的计入 llm_skipped 等下一轮/离线脚本补。"""
    company = make_company(session)
    hit = make_job(session, company, "J1", "Python 后端", "熟悉 Python")
    miss = make_job(session, company, "J2", "文案策划", "负责剧情与世界观")
    calls: list[str] = []
    monkeypatch.setattr(job_tags, "llm_extract_job_skills", lambda text: calls.append(text) or [])

    stats = tag_jobs(session, [hit, miss], ALIAS, use_llm=False, cap=10)

    assert stats == {"dict": 1, "llm": 0, "llm_failed": 0, "llm_skipped": 1}
    assert hit.skills == ["python"] and miss.skills == []
    assert calls == []  # 词典阶段没碰 LLM


def test_tag_jobs_llm_fallback_canonicalizes(session, monkeypatch):
    """LLM 兜底：原文写法要过 canonicalize（K8s → kubernetes）后才入库。"""
    company = make_company(session)
    job = make_job(session, company, "J1", "平台工程师", "负责内部平台研发")
    monkeypatch.setattr(job_tags, "llm_extract_job_skills", lambda text: ["K8s", "Python"])
    monkeypatch.setattr(job_tags.settings, "llm_api_key", "sk-test")

    stats = tag_jobs(session, [job], ALIAS, use_llm=True, cap=10)

    assert stats == {"dict": 0, "llm": 1, "llm_failed": 0, "llm_skipped": 0}
    assert job.skills == ["kubernetes", "python"]


def test_tag_jobs_respects_cap(session, monkeypatch):
    """cap 是每轮成本上限：超出的职位本轮不抽，计入 llm_skipped。"""
    company = make_company(session)
    jobs = [make_job(session, company, f"J{i}", "平台工程师", "负责内部平台研发") for i in range(3)]
    monkeypatch.setattr(job_tags, "llm_extract_job_skills", lambda text: ["Python"])
    monkeypatch.setattr(job_tags.settings, "llm_api_key", "sk-test")

    stats = tag_jobs(session, jobs, ALIAS, use_llm=True, cap=1)

    assert stats == {"dict": 0, "llm": 1, "llm_failed": 0, "llm_skipped": 2}
    assert [bool(j.skills) for j in jobs] == [True, False, False]


def test_tag_jobs_does_not_call_llm_without_key(session, monkeypatch):
    """未配 llm_api_key 时不发请求（与 §6 简历解析同一降级口径）。"""
    company = make_company(session)
    job = make_job(session, company, "J1", "平台工程师", "负责内部平台研发")
    monkeypatch.setattr(job_tags.settings, "llm_api_key", "")
    monkeypatch.setattr(
        job_tags, "llm_extract_job_skills", lambda text: (_ for _ in ()).throw(AssertionError("不应调用"))
    )

    stats = tag_jobs(session, [job], ALIAS, use_llm=True, cap=10)

    assert stats == {"dict": 0, "llm": 0, "llm_failed": 0, "llm_skipped": 1}


def test_tag_jobs_llm_failure_is_tolerated(session, monkeypatch):
    """单条 LLM 失败只计数，不抛异常、不影响其它职位（留空等下一轮补）。"""
    company = make_company(session)
    bad = make_job(session, company, "J1", "平台工程师", "负责内部平台研发")
    good = make_job(session, company, "J2", "数据工程师", "负责数据管道研发")

    def fake(text: str) -> list[str]:
        if "平台" in text:
            raise RuntimeError("llm 502")
        return ["Python"]

    monkeypatch.setattr(job_tags, "llm_extract_job_skills", fake)
    monkeypatch.setattr(job_tags.settings, "llm_api_key", "sk-test")

    stats = tag_jobs(session, [bad, good], ALIAS, use_llm=True, cap=10)

    assert stats == {"dict": 0, "llm": 1, "llm_failed": 1, "llm_skipped": 0}
    assert bad.skills == [] and good.skills == ["python"]


def test_tag_jobs_skips_jobs_without_description(session, monkeypatch):
    """无正文既不扫也不喂 LLM（如 SR 未开详情 enrich），避免拿标题瞎猜。"""
    company = make_company(session)
    job = make_job(session, company, "J1", "Python 工程师", None)
    monkeypatch.setattr(job_tags, "llm_extract_job_skills", lambda text: ["Python"])
    monkeypatch.setattr(job_tags.settings, "llm_api_key", "sk-test")

    stats = tag_jobs(session, [job], ALIAS, use_llm=True, cap=10)

    assert stats == {"dict": 0, "llm": 0, "llm_failed": 0, "llm_skipped": 0}
    assert job.skills == []


def test_tag_jobs_keeps_existing_skills(session):
    """已有标签的职位不重复处理（增量抽取，不重复花钱）。"""
    company = make_company(session)
    job = make_job(session, company, "J1", "Python 后端", "熟悉 Python", skills=["java"])

    stats = tag_jobs(session, [job], ALIAS, use_llm=True, cap=10)

    assert stats == {"dict": 0, "llm": 0, "llm_failed": 0, "llm_skipped": 0}
    assert job.skills == ["java"]


def test_build_alias_map_used_by_tagging(session):
    """抽标签与采集共用同一别名表（同一口径，避免两套归一漂移）。"""
    company = make_company(session)
    job = make_job(session, company, "J1", "Platform Engineer", "We run k8s in production")

    stats = tag_jobs(session, [job], build_alias_map(session))

    assert stats["dict"] == 1 and job.skills == ["kubernetes"]


def test_collect_tags_round_jobs(session, monkeypatch):
    """采集编排接入：本轮职位入库后自动补标签，结果里带 tagged 统计。"""
    from tests.test_collect import make_norm

    job = make_norm("J1", "Python 后端工程师")
    job.description = "熟悉 Python 与 K8s"

    r = run_collect(session, monkeypatch, [job])

    assert r["tagged"] == {"dict": 1, "llm": 0, "llm_failed": 0, "llm_skipped": 0}
    # 标题里的"后端"也会命中中文别名（backend），与 description 的命中一起归一入库
    stored = session.query(Job).filter(Job.external_id == "J1").one()
    assert stored.skills == ["backend", "kubernetes", "python"]


def test_collect_llm_off_by_default(session, monkeypatch):
    """默认不调 LLM：词典命不中时只标 llm_skipped，成本可控。"""
    from tests.test_collect import make_norm

    job = make_norm("J1", "文案策划")
    job.description = "负责剧情与世界观"
    monkeypatch.setattr(job_tags.settings, "job_tag_llm", False)

    r = run_collect(session, monkeypatch, [job])

    assert r["tagged"] == {"dict": 0, "llm": 0, "llm_failed": 0, "llm_skipped": 1}