"""适配器单测样例数据（对齐真实接口字段）。"""

from __future__ import annotations

from app.adapters.base import RawJob

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 6606581,
            "title": "Full Stack Engineer, Money as a Service",
            "absolute_url": "https://job-boards.greenhouse.io/stripe/jobs/6606581",
            "location": {"name": "Tokyo, Japan"},
            "updated_at": "2026-09-10T12:00:00-04:00",
            # content=true 时返回：实体转义后的 HTML（&lt;div&gt; …）
            "content": (
                "&lt;div class=&quot;content-intro&quot;&gt;&lt;h2&gt;Join us&lt;/h2&gt;"
                "&lt;ul&gt;&lt;li&gt;Python &amp; Go&lt;/li&gt;&lt;li&gt;Kubernetes, AWS&lt;/li&gt;"
                "&lt;/ul&gt;&lt;/div&gt;"
            ),
        },
        {
            "id": 8023773,
            "title": "AI Product Manager, Professional Services",
            "absolute_url": "https://job-boards.greenhouse.io/stripe/jobs/8023773",
            "location": {"name": "Remote, US"},
            "updated_at": "2026-09-14T09:30:00-04:00",
            "content": None,  # 无全文：description 应为 None 而非空串
        },
    ]
}

LEVER_PAYLOAD = [
    {
        "id": "a1b2c3d4",
        "text": "Software Engineer, Media",
        "hostedUrl": "https://jobs.lever.co/twitch/a1b2c3d4",
        "createdAt": 1789344000000,  # 2026-09-14 UTC
        "categories": {"location": "Seattle, WA", "team": "Engineering", "commitment": "Full-time"},
        "descriptionPlain": "Build live video systems.",
    },
    {
        "id": "e5f6a7b8",
        "text": "Senior Data Scientist",
        "hostedUrl": "https://jobs.lever.co/twitch/e5f6a7b8",
        "createdAt": 1789257600000,  # 2026-09-13 UTC
        "categories": {"location": "San Francisco, CA", "team": "Data"},
        "descriptionPlain": None,
    },
]

WORKDAY_PAGE_1 = {
    "total": 25,
    "jobPostings": [
        {
            "title": "Director, Engineering - Software Engineering",
            "externalPath": "/job/Vietnam-Hanoi/Director--Engineering---Software-Engineering_JR2021061",
            "locationsText": "Vietnam, Hanoi",
            "bulletFields": ["CUDA", "K8s", "Distributed Systems"],
        },
        {
            "title": "Software Engineer, Graphics",
            "externalPath": "/job/Santa-Clara/Software-Engineer_Graphics_JR1999999",
            "locationsText": "Santa Clara, CA",
            "bulletFields": ["Vulkan"],
        },
    ]
    + [
        {
            "title": f"Bulk Role {i}",
            "externalPath": f"/job/Austin/Bulk-Role_{i}_JR10000{i}",
            "locationsText": "Austin, TX",
            "bulletFields": [],
        }
        for i in range(18)  # 凑满 20 条/页
    ],
}

WORKDAY_PAGE_2 = {
    "total": 25,
    "jobPostings": [
        {
            "title": "Senior Software Engineer, AI Inference",
            "externalPath": "/job/Santa-Clara/Senior-Software-Engineer_JR2000002",
            "locationsText": "Santa Clara, CA",
            "bulletFields": ["Python", "PyTorch"],
        }
    ],
}

# 网易 /api/hr163/position/queryPage 响应样例（字段对齐 2026-09-16 实测）
NETEASE_PAGE_1 = {
    "code": 200,
    "msg": None,
    "data": {
        "pages": 2,
        "total": 3,
        "list": [
            {
                "id": 58384,
                "name": "平台开发实习生（Python方向）",
                "beeUrl": None,
                "workPlaceNameList": ["杭州市"],
                "workPlaceList": [229],
                "reqEducationName": "本科",
                "reqWorkYearsName": "不限",
                "firstDepName": "雷火事业群",
                "firstPostTypeName": "技术",
                "recruitNum": 1,
                "workType": "1",
                "updateTime": 1789480785000,  # 2026-09-15 UTC
                "description": "负责平台开发。",
                "requirement": "熟悉 Python。",
            },
            {
                "id": 69515,
                "name": "高级/资深文案策划（修仙新项目）",
                "beeUrl": "https://hr.163.com/job-detail.html?id=69515",
                "workPlaceNameList": ["杭州市", "广州"],
                "reqEducationName": "不限",
                "reqWorkYearsName": "3-5年",
                "updateTime": 1789568801000,  # 2026-09-16 UTC
                "description": "搭建世界观。",
                "requirement": None,
            },
        ],
    },
}

NETEASE_PAGE_2 = {
    "code": 200,
    "msg": None,
    "data": {
        "pages": 2,
        "total": 3,
        "list": [
            {
                "id": 70001,
                "name": "服务端开发工程师（Java）",
                "beeUrl": None,
                "workPlaceNameList": ["北京"],
                "reqEducationName": "硕士",
                "reqWorkYearsName": "1年以下",
                "updateTime": None,
                "description": None,
                "requirement": None,
            }
        ],
    },
}


def greenhouse_raws() -> list[RawJob]:
    return [RawJob(payload=j, company_slug="stripe") for j in GREENHOUSE_PAYLOAD["jobs"]]


def lever_raws() -> list[RawJob]:
    return [RawJob(payload=j, company_slug="twitch") for j in LEVER_PAYLOAD]


def workday_raws() -> list[RawJob]:
    return [
        RawJob(
            payload=p,
            company_slug="nvidia",
            feed_url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
        )
        for p in WORKDAY_PAGE_1["jobPostings"]
    ]


def netease_raws() -> list[RawJob]:
    return [
        RawJob(payload=j, company_slug="netease", feed_url="https://hr.163.com")
        for j in NETEASE_PAGE_1["data"]["list"] + NETEASE_PAGE_2["data"]["list"]
    ]
