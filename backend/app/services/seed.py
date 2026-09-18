"""首批公司种子（开发文档 §5.3 定稿名单）与技能字典种子。幂等：按 slug/canonical upsert。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, SkillTag

COMPANY_SEED: list[dict] = [
    {
        "slug": "nvidia",
        "name": "NVIDIA",
        "ats_type": "workday",
        "site_url": "https://jobs.nvidia.com/careers",
        "feed_url": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        "slug": "stripe",
        "name": "Stripe",
        "ats_type": "greenhouse",
        "site_url": "https://stripe.com/jobs",
        "feed_url": "https://boards-api.greenhouse.io/v1/boards/stripe/jobs",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        # 2026-09-16 实测：Twitch/Anyscale 等 Lever 板均已迁移（Anyscale 仅剩 1 条搬家公告），
        # Lever 降级为"适配器就绪、无 M1 代表公司"；第三家改用 Greenhouse 的 Robinhood（153 条实测）。
        "slug": "robinhood",
        "name": "Robinhood",
        "ats_type": "greenhouse",
        "site_url": "https://careers.robinhood.com",
        "feed_url": "https://boards-api.greenhouse.io/v1/boards/robinhood/jobs",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        # 2026-09-18 实测：Ashby posting-api 免鉴权单页全量（ashby 73 / elevenlabs 236 /
        # linear 32 条），列表即带 descriptionPlain，零 N+1（见 app/adapters/ashby.py）。
        "slug": "ashby",
        "name": "Ashby",
        "ats_type": "ashby",
        "site_url": "https://jobs.ashbyhq.com/ashby",
        "feed_url": "https://jobs.ashbyhq.com/ashby",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        "slug": "elevenlabs",
        "name": "ElevenLabs",
        "ats_type": "ashby",
        "site_url": "https://jobs.ashbyhq.com/elevenlabs",
        "feed_url": "https://jobs.ashbyhq.com/elevenlabs",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        "slug": "linear",
        "name": "Linear",
        "ats_type": "ashby",
        "site_url": "https://jobs.ashbyhq.com/linear",
        "feed_url": "https://jobs.ashbyhq.com/linear",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        # 2026-09-18 实测：SmartRecruiters 公开 API v1（Equinox totalFound=736），
        # 列表无 description（详情 N+1 一期不取，见 app/adapters/smartrecruiters.py）。
        "slug": "equinox",
        "name": "Equinox",
        "ats_type": "smartrecruiters",
        "site_url": "https://jobs.smartrecruiters.com/Equinox",
        "feed_url": "https://jobs.smartrecruiters.com/Equinox",
        "locale": "en-US",
        "fetch_policy": {"interval_min": 360},
    },
    {
        # 2026-09-16 实测：hr.163.com 公开 JSON 接口免鉴权无加密，
        # NeteaseAdapter 已落地（见 app/adapters/netease.py 模块 docstring）。
        "slug": "netease",
        "name": "网易",
        "ats_type": "netease",
        "site_url": "https://hr.163.com",
        "feed_url": "https://hr.163.com",
        "locale": "zh-CN",
        "fetch_policy": {"interval_min": 720},
    },
    {
        # 2026-09-16 实测：大疆招聘实为 Moka ATS（apply.careers.dji.com，siteId=170070），
        # 公开列表接口 jobs/v2 等响应整体加密（{"data":"<密文>","necromancer":"..."}），
        # 解密需复刻其前端混淆逻辑，属绕过反爬措施，不符合 §5.2 合规原则 → 暂缓。
        # 行保留但休眠；we.dji.com 仅是品牌门面（无 SSR 职位数据、无 sitemap）。
        "slug": "dji",
        "name": "大疆 DJI",
        "ats_type": "official_site",
        "site_url": "https://we.dji.com",
        "feed_url": None,
        "locale": "zh-CN",
        "fetch_policy": {"interval_min": 720},
        "is_active": False,
    },
]

SKILL_SEED: list[tuple[str, str, list[str]]] = [
    ("kubernetes", "skill", ["k8s", "kube"]),
    ("go", "skill", ["golang"]),
    ("javascript", "skill", ["js", "nodejs", "node.js"]),
    ("python", "skill", ["py"]),
    ("java", "skill", []),
    ("docker", "skill", []),
    ("react", "skill", []),
    ("sql", "skill", []),
    ("aws", "skill", []),
    ("machine learning", "skill", ["ml", "人工智能", "机器学习"]),
    ("backend", "role", ["后端"]),
]


def run_seed(session: Session) -> dict:
    companies = 0
    for item in COMPANY_SEED:
        existing = session.execute(select(Company).where(Company.slug == item["slug"])).scalars().first()
        if existing is None:
            session.add(Company(**item))
            companies += 1
        else:
            for key, value in item.items():
                setattr(existing, key, value)

    tags = 0
    for canonical, category, aliases in SKILL_SEED:
        existing = session.execute(select(SkillTag).where(SkillTag.canonical == canonical)).scalars().first()
        if existing is None:
            session.add(SkillTag(canonical=canonical, category=category, aliases=aliases))
            tags += 1
        else:
            existing.aliases = aliases

    session.commit()
    return {"companies_upserted": len(COMPANY_SEED), "companies_inserted": companies, "skill_tags_inserted": tags}
