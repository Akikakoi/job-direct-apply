"""语义融合打分测试（P4）：tokenize、TF-IDF 余弦、融合公式、中性分回归。"""

from __future__ import annotations

import pytest

from app.pipelines.match import compute_match, refresh_matches
from app.pipelines.semantic import (
    TfidfIndex,
    cross_lingual_tokens,
    job_text,
    resume_query_text,
    tokenize,
)
from app.models import Job, MatchScore, Resume


def test_tokenize_mixed():
    toks = tokenize("Python 后端开发 Backend")
    assert "python" in toks
    assert "backend" in toks
    assert "后端" in toks and "端开" in toks and "开发" in toks  # CJK 2-gram


def test_cross_lingual_bridge_tokens():
    """§12.7 #9 C：术语桥双向补词，且 ASCII 侧按词边界匹配不误命中子串。"""
    assert "backend" in tokenize("高级后端开发工程师")  # 中文术语 → 补英文写法
    assert "后端" in tokenize("Senior Backend Engineer")  # 英文术语 → 补中文核心词
    assert "web" not in tokenize("Build websites")  # 词边界："web" 不命中 "websites"
    assert cross_lingual_tokens("") == []


def test_cross_lingual_bridge_raises_overseas_similarity():
    """中文简历 vs 英文 JD：桥接后语义分不再恒为 0，且相关 > 无关。"""
    cn_resume = "后端开发工程师，熟悉 Python、MySQL、Redis，负责订单系统"
    related = "Senior Backend Engineer - Python, MySQL, Redis. You will own our order platform."
    unrelated = "Senior Brand Marketing Manager, Retail Campaigns"
    idx = TfidfIndex().fit([related, unrelated])
    q = idx.build_query(cn_resume)
    assert idx.similarity(0, q) > 0  # 桥接生效（纯字面重叠时该值会趋近 0）
    assert idx.similarity(0, q) > idx.similarity(1, q)


def test_tfidf_similarity_related_vs_unrelated():
    docs = [
        "Python 后端开发工程师，熟悉 FastAPI、MySQL、Redis",
        "资深游戏文案策划，负责剧情与 NPC 配音包装",
        "高级服务端开发工程师（Java 方向）",
    ]
    idx = TfidfIndex().fit(docs)
    q = idx.build_query("后端开发工程师 Python MySQL")
    backend_sim = idx.similarity(0, q)
    writer_sim = idx.similarity(1, q)
    java_sim = idx.similarity(2, q)
    assert backend_sim > writer_sim  # 相关 > 无关
    assert java_sim > writer_sim  # 同域 > 无关


def test_fusion_formula():
    class FakeJob:  # 避免造库，只测公式
        title = "后端工程师"
        skills = ["python"]
        city = "杭州市"
        experience_min = None
        description = None

    profile = {"skills": ["python"], "target_role": "后端", "cities": ["杭州"], "experience_years": 5}
    result = compute_match(profile, FakeJob(), vec_score=0.5)
    # rule=1.0（四分项全 1），final = 0.75*1 + 0.25*0.5 = 0.875
    assert result["rule"] == pytest.approx(1.0)
    assert result["score"] == pytest.approx(0.875)
    semantic = next(e for e in result["explain"] if e["key"] == "semantic")
    assert semantic["score"] == 0.5


def test_semantic_beats_neutral_skill_job(session):
    """回归：skills 为空 + 文本无关的职位，融合后应被压到命中职位之下。"""
    good = Job(
        external_id="t-good",
        title="后端开发工程师（Python 方向）",
        city="杭州市",
        skills=["python", "mysql"],
        description="负责订单系统后端开发，技术栈 Python FastAPI MySQL",
        apply_url="https://e.com/good",
        source="test",
        status="active",
    )
    noisy = Job(
        external_id="t-noisy",
        title="Account Executive, Enterprise",
        city="US-Remote",
        skills=[],
        description=None,
        apply_url="https://e.com/noisy",
        source="test",
        status="active",
    )
    session.add_all([good, noisy])
    session.add(
        Resume(
            user_id=1,
            raw_text="Python 后端开发工程师，6 年经验，熟悉 FastAPI MySQL",
            profile={"skills": ["python", "mysql"], "target_role": "后端工程师", "cities": ["杭州"], "experience_years": 6},
            lang="zh",
        )
    )
    session.commit()

    resume = session.query(Resume).first()
    refresh_matches(session, resume)
    scores = {m.job_id: (m.final_score, m.rule_score) for m in session.query(MatchScore)}
    assert scores[good.id][0] > scores[noisy.id][0]  # 融合分拉开差距
    # 规则分层面 noisy 靠中性分蹭到不低的值，但融合分被打回
    assert scores[noisy.id][1] >= scores[noisy.id][0] or scores[good.id][0] > scores[noisy.id][1]


def test_empty_profile_falls_back_to_rule(session):
    """简历无有效文本 → 退纯规则分（不融合）。"""
    job = Job(
        external_id="t-x",
        title="任意职位",
        city=None,
        skills=[],
        apply_url="https://e.com/x",
        source="test",
        status="active",
    )
    session.add(job)
    session.add(Resume(user_id=1, raw_text="", profile={}, lang="zh"))
    session.commit()
    resume = session.query(Resume).first()
    refresh_matches(session, resume)
    m = session.query(MatchScore).first()
    assert m.vec_score is None
    assert m.final_score == m.rule_score
    assert all(e["key"] != "semantic" for e in m.explain)


def test_resume_query_text_prefers_structured():
    text = resume_query_text({"skills": ["python"], "target_role": "后端"}, "原始简历噪音" * 100)
    assert "python" in text and "后端" in text
    assert len(text) < 3300  # 原文截断到 3000
