"""LLM 结构化抽取客户端（P2 §6）。

DeepSeek / OpenAI 兼容 chat.completions 接口，response_format=json_object。
未配置 llm_api_key 时抛 LLMNotConfigured，由 parse.py 退规则兜底。
"""

from __future__ import annotations

import json

import httpx

from app.core.config import settings

SYSTEM_PROMPT = """你是简历解析器。从简历文本中抽取结构化信息，只输出 JSON，字段：
skills: string[] 技能列表（保留简历原文写法，如 "K8s"、"FastAPI"）
experience_years: integer 总工作年限（数字）
target_role: string 求职意向/目标职位
cities: string[] 期望工作城市
salary_min: number 期望月薪下限（单位 K）
salary_max: number 期望月薪上限（单位 K）
industry: string[] 期望行业
edu_degree: "phd"|"master"|"bachelor"|"associate"
不确定的字段直接省略，禁止编造。"""


class LLMNotConfigured(Exception):
    """llm_api_key 未配置。"""


def _build_client() -> httpx.Client:
    if not settings.llm_api_key:
        raise LLMNotConfigured("llm_api_key 未配置")
    return httpx.Client(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=settings.llm_timeout_s,
    )


def llm_extract_profile(text: str) -> dict:
    """调用 LLM 抽取 profile；返回 dict（仅含 LLM 认为可信的字段）。"""
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:12000]},  # 截断防超上下文
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    with _build_client() as client:
        resp = client.post("/chat/completions", json=payload)
        resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM 返回非 JSON 对象")
    return data
