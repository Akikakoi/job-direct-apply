"""简历解析管道（P2，对应开发文档 §6）。

流程：上传/文本抽取 → 清洗 → LLM 结构化抽取（无 key 时规则兜底）
→ 规则校验合并（防幻觉）→ skills 归一到 skill_tags 口径 → profile dict。

profile 结构（§6）：
{
  "skills": [...], "experience_years": int, "target_role": str,
  "cities": [...], "salary_min": K, "salary_max": K,
  "industry": [...], "edu_degree": "bachelor|master|phd|associate",
  "lang": "zh|en", "source": "llm|rules", "notes": [...]
}
"""

from __future__ import annotations

from app.pipelines.llm_extract import LLMNotConfigured, llm_extract_profile
from app.pipelines.rules import extract_profile_rules, validate_profile
from app.services.collect import build_alias_map, canonicalize_skills

__all__ = ["parse_resume_text", "LLMNotConfigured"]


def clean_text(text: str) -> str:
    """文本清洗：统一换行、去零宽字符、压缩多余空白。"""
    import re

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_resume_text(raw_text: str, session, use_llm: bool | None = None) -> dict:
    """解析简历文本 → profile dict。

    use_llm=None 时自动判断：配置了 llm_api_key 才走 LLM。
    LLM 抽取后仍叠加规则校验（§6 防幻觉：数值字段以规则为准的冲突记入 notes）。
    """
    from app.core.config import settings

    text = clean_text(raw_text)
    if not text:
        raise ValueError("简历文本为空")

    notes: list[str] = []
    alias_map = build_alias_map(session)
    rule_profile = extract_profile_rules(text, alias_map)

    source = "rules"
    llm_profile: dict | None = None
    if use_llm is None:
        use_llm = bool(settings.llm_api_key)
    if use_llm:
        try:
            llm_profile = llm_extract_profile(text)
            source = "llm"
        except LLMNotConfigured:
            notes.append("llm_not_configured_fallback_rules")
        except Exception as exc:  # 网络/解析失败不阻塞入库，退规则兜底
            notes.append(f"llm_failed_fallback_rules: {type(exc).__name__}")

    profile = validate_profile(llm_profile, rule_profile, notes) if llm_profile else dict(rule_profile)

    # skills 归一到 skill_tags 字典口径（§6 口径统一）：LLM 技能 ∪ 词典扫描命中
    llm_skills = list(profile.get("skills") or [])
    scanned = list(rule_profile.get("skills") or [])
    profile["skills"] = canonicalize_skills(llm_skills + scanned, alias_map)

    profile["source"] = source
    profile["lang"] = rule_profile.get("lang", "zh")
    if notes:
        profile["notes"] = notes
    return profile
