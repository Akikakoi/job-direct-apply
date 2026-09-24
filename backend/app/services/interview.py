"""面试陪伴闭环（§12.6 P5 ②）：面试准备清单 / 公司背景包 / 面试定时催进。

产物全部**离线可算**（规则推导，不调 LLM、不触网）：

- **准备清单（checklist）**：材料项（对齐 JD 的简历版本、项目复盘、反问问题、面试
  形式与时间确认）+ 由"职位要求 vs 简历画像"推导的补强项；每项带 `done` 供前端打勾；
- **技能考察点（skill_points）**：按职位技能逐个给准备口径——简历命中 → 用项目
  量化串讲；未命中 → 先补基础概念，并准备"可迁移经验/正在学习"的**诚实口径**
  （不教用户谎报技能）；
- **公司背景包（company_pack）**：只用库内**公开招聘信息**（公司在库 active 职位数、
  ATS 来源、官网地址、职位城市与来源），不做任何未公开情报拼接；
- **定时催进**：`applications.interview_at` 是用户登记的面试时间（可空）。beat 每日
  扫 `status=interview` 且面试时间落在近 `within_days` 天窗口内的申请推送提醒；面试
  时间已过但结果未更新的（`_OVERDUE_GRACE_DAYS` 天内）继续提醒"补记结果"，超期不再
  骚扰（否则终态迟迟不更新会变成永远打扰）。

**已收敛的边界**：清单/背景包/催进扫描/接口全部离线可测。**剩余条件（非代码）**：
公司深度资料（融资轮次/新闻）需外部数据源；日程双向同步需日历 OAuth 授权；提醒的
邮件/IM 通道复用 `notify.py`（未配置则降级打日志）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, Company, Job, Resume

# 面试时间已过、但结果尚未更新的宽限天数（超期不再提醒，避免终态不更新导致永久打扰）
_OVERDUE_GRACE_DAYS = 7
# 催进默认前瞻窗口（天）：面试前 N 天进入提醒
DEFAULT_WITHIN_DAYS = 2

_CJK = re.compile(r"[\u4e00-\u9fff]")
# 可登记面试时间的状态（终态无意义；rejected/closed/offer/no_feedback 由状态机定义）
_SETTABLE_STATUSES = ("submitted", "under_review", "interview")


def _as_naive(dt: datetime | None) -> datetime | None:
    """统一成 naive UTC——SQLite 存 naive、PG 可能回 aware，混算会 TypeError。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _days_left(interview_at: datetime | None, now: datetime) -> int | None:
    """距面试还有几天（向下取整，已过为负）；无时间 → None。"""
    naive = _as_naive(interview_at)
    if naive is None:
        return None
    return int((naive - now).total_seconds() // 86400)


def is_english_job(job: Job | None) -> bool:
    """职位是否以英文为主（title + description 的 ASCII 字母显著多于 CJK）。"""
    if job is None:
        return False
    text = f"{job.title or ''} {job.description or ''}"
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    cjk = len(_CJK.findall(text))
    return ascii_letters >= 20 and ascii_letters > cjk * 3


def _company_pack(session: Session, job: Job | None) -> dict:
    """公司背景包：只用库内公开招聘信息，不拼外部未公开情报。"""
    if job is None:
        return {"name": None, "ats_type": None, "site_url": None, "open_jobs": 0,
                "job_city": None, "job_source": None, "talking_points": []}
    company = session.get(Company, job.company_id) if job.company_id else None
    open_jobs = 0
    if company is not None:
        open_jobs = session.execute(
            select(func.count()).select_from(Job).where(
                Job.company_id == company.id, Job.status == "active"
            )
        ).scalar_one()
    points: list[str] = []
    if company is not None:
        points.append(
            f"该公司在库有 {open_jobs} 个在招职位（来源：{company.ats_type}）——"
            "在招数量与方向可反推团队扩张重点，面试时可据此提问"
        )
        if company.site_url:
            points.append(f"官网/招聘页：{company.site_url}（建议提前看业务线与工程博客各 1~2 篇）")
    if job.city:
        points.append(f"职位城市 {job.city}：确认办公地点、是否支持远程/混合")
    if job.publish_date:
        points.append(f"职位发布于 {job.publish_date}：可问「这个岗位是新增编制还是替补」")
    return {
        "name": company.name if company else None,
        "ats_type": company.ats_type if company else None,
        "site_url": company.site_url if company else None,
        "open_jobs": open_jobs,
        "job_city": job.city,
        "job_source": job.source,
        "talking_points": points,
    }


def _skill_points(job_skills: list[str], resume_skills: set[str]) -> list[dict]:
    """按职位技能给准备口径（命中→量化串讲；缺口→补基础 + 诚实口径）。"""
    points: list[dict] = []
    for skill in job_skills:
        have = skill.lower() in resume_skills
        if have:
            hint = f"简历已体现：准备一个量化案例（规模/性能/收益 至少给一个数字）串讲 {skill}"
        else:
            hint = (
                f"简历未体现：先补 {skill} 的基础概念与常见问题，"
                "再准备「相关可迁移经验 + 正在学习」的诚实口径（切勿声称精通）"
            )
        points.append({"skill": skill, "have": have, "hint": hint})
    return points


def _checklist(job: Job | None, profile: dict, gaps: list[str], english: bool) -> list[dict]:
    """面试准备清单（固定材料项 + 画像相关补强项）。"""
    items = [
        {
            "key": "resume_aligned",
            "title": "准备与 JD 对齐的简历版本",
            "detail": "把 JD 里的关键词自然落到项目描述中（不要堆砌），并打印/导出 PDF 备用",
            "done": bool(profile.get("skills")),
        },
        {
            "key": "project_stories",
            "title": "为每个命中技能准备一个量化案例",
            "detail": "STAR 结构：背景 → 任务 → 行动 → 结果（结果尽量带数字）",
            "done": False,
        },
        {
            "key": "gap_sprint",
            "title": f"缺口技能速通：{'、'.join(gaps[:3])}" if gaps else "缺口技能自查：当前无未覆盖的职位技能",
            "detail": "每个缺口按「概念 → 最小 demo → 常见追问」三步准备" if gaps else "仍建议按技能点通读一遍 JD 措辞",
            "done": not gaps,
        },
        {
            "key": "logistics",
            "title": "确认面试形式与时间",
            "detail": "线上（链接/设备/网络）或线下（地址/路线），并换算好时区与提前量",
            "done": False,
        },
        {
            "key": "questions",
            "title": "准备 3 个反问问题",
            "detail": "围绕团队目标、技术栈演进、入职 6 个月的期望（避免只问薪资福利）",
            "done": False,
        },
    ]
    if english:
        items.append(
            {
                "key": "english_self_intro",
                "title": "准备英文自我介绍与项目讲解",
                "detail": "3 分钟版 + 1 分钟版各一套，重点练项目细节的英文表达",
                "done": False,
            }
        )
    if job is not None and job.city:
        items.append(
            {
                "key": "location",
                "title": f"确认工作地点与远程政策（{job.city}）",
                "detail": "办公地点、是否支持远程/混合、通勤安排",
                "done": False,
            }
        )
    return items


def build_interview_kit(session: Session, application: Application, now: datetime | None = None) -> dict:
    """生成面试陪伴包：准备清单 + 技能考察点 + 公司背景包 + 时间线。"""
    now = now or datetime.utcnow()
    job = session.get(Job, application.job_id) if application.job_id else None
    resume = session.get(Resume, application.resume_id) if application.resume_id else None
    profile = dict(resume.profile or {}) if resume is not None else {}

    job_skills = [str(s) for s in (job.skills or [])] if job is not None else []
    resume_skills = {str(s).lower() for s in (profile.get("skills") or [])}
    strengths = [s for s in job_skills if s.lower() in resume_skills]
    gaps = [s for s in job_skills if s.lower() not in resume_skills]
    english = is_english_job(job)
    days_left = _days_left(application.interview_at, now)

    questions = [
        "团队当前最想解决的技术/业务问题是什么？入职 6 个月内期望我交付什么？",
        "这个岗位是新增编制还是替补？团队规模与协作方式（评审/排期节奏）如何？",
        "技术栈近一年的演进方向？是否有机会参与架构/工具链建设？",
        "绩效评估与成长路径如何定义（晋升周期、导师制、内部转岗）？",
    ]

    notes = [
        "清单与考察点由「职位要求 vs 简历画像」规则推导，属准备提示而非面试预测。",
        "公司背景包只用库内公开招聘信息（在招数量/官网/城市），不含未公开情报。",
    ]
    if not job_skills:
        notes.append("该职位无 skills 标签（词典未命中且 LLM 兜底未开启），技能考察点为空——建议先读 JD 原文自行归纳。")
    if application.interview_at is None:
        notes.append("未登记面试时间：登记后（PUT /api/applications/{id}/interview-at）才会进入定时催进。")

    return {
        "application": {
            "id": application.id,
            "user_id": application.user_id,
            "status": application.status,
            "job_id": application.job_id,
            "job_title": job.title if job is not None else None,
            "apply_url": application.apply_url or (job.apply_url if job is not None else None),
            "interview_at": str(application.interview_at) if application.interview_at else None,
            "days_left": days_left,
        },
        "checklist": _checklist(job, profile, gaps, english),
        "skill_points": _skill_points(job_skills, resume_skills),
        "strengths": strengths,
        "gaps": gaps,
        "english_interview": english,
        "reverse_questions": questions,
        "company_pack": _company_pack(session, job),
        "notes": notes,
    }


def set_interview_at(session: Session, application: Application, interview_at: datetime | None) -> Application:
    """登记/清空面试时间；终态申请拒绝登记（返回的是已提交的 application）。"""
    if application.status not in _SETTABLE_STATUSES:
        raise ValueError(f"当前状态 {application.status} 为终态，无需登记面试时间")
    application.interview_at = interview_at
    session.commit()
    return application


def scan_interview_reminders(
    session: Session,
    now: datetime | None = None,
    within_days: int = DEFAULT_WITHIN_DAYS,
    user_id: int | None = None,
) -> list[dict]:
    """面试催进扫描：interview 状态 + 已登记时间 + 落在 [now-宽限, now+前瞻] 窗口内。

    已过面试时间的条目标 `overdue=True`（提示补记结果），超过 `_OVERDUE_GRACE_DAYS`
    天的不再出现，避免状态迟迟不更新导致永久打扰。
    """
    now = now or datetime.utcnow()
    stmt = (
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(
            Application.status == "interview",
            Application.interview_at.isnot(None),
            Application.interview_at <= now + timedelta(days=within_days),
            Application.interview_at >= now - timedelta(days=_OVERDUE_GRACE_DAYS),
        )
    )
    if user_id is not None:
        stmt = stmt.where(Application.user_id == user_id)
    rows = session.execute(stmt.order_by(Application.interview_at.asc())).all()
    items: list[dict] = []
    for app, job in rows:
        left = _days_left(app.interview_at, now)
        items.append(
            {
                "application_id": app.id,
                "user_id": app.user_id,
                "job_id": job.id,
                "job_title": job.title,
                "apply_url": app.apply_url or job.apply_url,
                "status": app.status,
                "interview_at": str(app.interview_at),
                "days_left": left,
                "overdue": bool(left is not None and left < 0),
            }
        )
    return items


def build_interview_body(items: list[dict]) -> str:
    """面试催进文案（邮件正文/IM 文本共用）。"""
    lines = [f"面试提醒：{len(items)} 场面试待准备", ""]
    for it in items:
        when = "已过" if it["overdue"] else "临近"
        left = it["days_left"]
        delta = f"{abs(left)} 天后" if left is not None and left >= 0 else f"已过 {abs(left)} 天" if left is not None else "时间未知"
        lines.append(f"- #{it['application_id']} {it['job_title']}（{when}：{it['interview_at']}，{delta}）\n  {it['apply_url']}")
    lines.append("")
    lines.append("建议：打开面试陪伴包（GET /api/applications/{id}/interview-kit）过一遍清单与技能考察点；面试结束后及时更新投递状态。")
    return "\n".join(lines)