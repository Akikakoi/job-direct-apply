"""采集入库阶段抽标签（§5.2 / §12.7 #7）。

顺序固定为"词典优先、LLM 兜底"：
1) 词典扫描（`rules.scan_skills` + 别名表）零成本、可离线跑，先吃掉能用现成字典命中的部分；
2) 剩下的空标签职位再走 LLM（`job_tag_llm` 开关 + `job_tag_llm_cap` 上限）——
   否则 LLM 会去处理"本来就该免费命中"的职位，成本白白翻倍。

设计约束（对应 §5.2）：
- 抽出的标签一律走 `canonicalize_skills` 归一到标准标签，与简历侧同一口径；
- **不阻塞入库**：调用方在职位/fetch_log 提交之后才调用本模块，单个职位抽取失败
  只计入 `llm_failed`，留空等下一轮采集或离线脚本补，绝不抛给采集主流程；
- 增量抽取：只处理"本轮采集到、且 skills 为空"的职位，已有标签的不重复花钱。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Job
from app.pipelines.llm_extract import _build_client
from app.pipelines.rules import scan_skills

# 职位侧 prompt 与简历侧分开：JD 里全是招聘方话术，软性词/福利/公司名必须显式排除，
# 否则 LLM 会把"沟通能力强""五险一金"当技能写进 skills，污染技能匹配。
SYSTEM_PROMPT = """你是招聘信息解析器。从职位描述中抽取招聘方要求的技术栈与专业能力标签，只输出 JSON：
{"skills": string[]}
要求：
1. 只抽可迁移的技术/工具/专业能力名词（如 Python、Kubernetes、SQL、用户增长、财务建模），保留原文写法（"K8s" 就写 "K8s"）；
2. 不要抽：公司名与产品名、福利待遇（五险一金/弹性工作/股票期权）、软性形容词（沟通能力强/有责任心/抗压）、
   职级、工作年限、学历要求、地点；
3. 没有可抽取内容时输出 {"skills": []}；不确定的不写，禁止编造。"""

MAX_SKILLS_PER_JOB = 30
MAX_TEXT_CHARS = 6000  # 截断防超上下文：JD 尾部多为福利条款，信息量低


def _canonicalize(skills: list[str], alias_map: dict[str, str]) -> list[str]:
    from app.services.collect import canonicalize_skills  # 局部导入打破 collect↔job_tags 循环

    return canonicalize_skills(skills, alias_map)


def tag_from_dictionary(title: str, description: str | None, alias_map: dict[str, str]) -> list[str]:
    """词典扫描（零成本）：title + description 里所有命中的标准标签。"""
    return _canonicalize(scan_skills(f"{title}\n{description or ''}", alias_map), alias_map)


def llm_extract_job_skills(text: str) -> list[str]:
    """调用 LLM 抽取职位技能标签（原文写法，未归一）；llm_api_key 空时抛 LLMNotConfigured。"""
    import json

    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:MAX_TEXT_CHARS]},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
    with _build_client() as client:
        resp = client.post("/chat/completions", json=payload)
        resp.raise_for_status()
    data = json.loads(resp.json()["choices"][0]["message"]["content"])
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        return []
    return [str(s).strip() for s in skills if isinstance(s, str) and str(s).strip()][:MAX_SKILLS_PER_JOB]


def tag_jobs(
    session: Session,
    jobs: list[Job],
    alias_map: dict[str, str],
    use_llm: bool = False,
    cap: int = 0,
) -> dict:
    """对传入职位抽标签并就地写回 `job.skills`（调用方负责 commit）。

    只处理 skills 为空且 description 非空的职位；返回统计
    `{dict, llm, llm_failed, llm_skipped}`，供 collect 结果与 fetch_log 观测。
    """
    stats = {"dict": 0, "llm": 0, "llm_failed": 0, "llm_skipped": 0}
    todo: list[Job] = []
    for job in jobs:
        if job.skills:
            continue
        if not (job.description or "").strip():
            continue  # 无正文：既扫不出也没法喂 LLM（如 SR 未开详情 enrich）
        hits = tag_from_dictionary(job.title, job.description, alias_map)
        if hits:
            job.skills = hits
            stats["dict"] += 1
        else:
            todo.append(job)

    if not use_llm or cap <= 0 or not settings.llm_api_key:
        stats["llm_skipped"] = len(todo)
        return stats

    batch = todo[:cap]
    stats["llm_skipped"] = len(todo) - len(batch)
    for job in batch:
        try:
            raw = llm_extract_job_skills(f"{job.title}\n{job.description}")
        except Exception:
            # 单条失败不影响其余职位，更不影响入库（调用方已先提交职位）
            stats["llm_failed"] += 1
            continue
        hits = _canonicalize(raw, alias_map)
        if hits:
            job.skills = hits
            stats["llm"] += 1
    session.flush()
    return stats