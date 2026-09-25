"""§7 二期 PGVector 多语嵌入：provider 门禁、向量存取、match/tuning 同源分流。

两层测试：
- 单测（SQLite，全离线）：默认 provider=none 走 TF-IDF 老路（P4 一期行为不变）；
  维度门禁与向量文本协议；非 PG 防御；match 分流（mock 就绪判定+provider，
  验证向量路径真正接管 match_scores 的 vec_score 与 explain 标注）。
- PG 集成（设 JDA_TEST_PG_URL 才跑，CI 无 PG 自动跳过）：真容器上验 ensure_column
  幂等 / upsert / `<=>` 检索顺序 / NULL 向量行不参与。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.models import Job, MatchScore, Resume, User
from app.pipelines.match import refresh_matches
from app.services import vector_store
from app.services.embeddings import SentenceTransformerProvider, get_provider

PG_URL = os.environ.get("JDA_TEST_PG_URL")


# ---------- 夹具与场景 ----------


@pytest.fixture(autouse=True)
def _no_llm_key(monkeypatch):
    """与 test_resume_parse / test_auth 同约定：本机 .env 有真 LLM key，必须封掉。"""
    monkeypatch.setattr(settings, "llm_api_key", "")


def _make_user(session, email: str) -> User:
    user = User(email=email, password_hash="x$1$y$z", role="user")
    session.add(user)
    session.commit()
    return user


def _make_resume(session, user: User) -> Resume:
    return Resume(
        user_id=user.id,
        raw_text="python 后端开发 三年经验",
        profile={
            "skills": ["python"],
            "cities": ["杭州"],
            "target_role": "后端",
        },
        is_active=True,
    )


def _make_job(session, external_id: str, title: str) -> Job:
    job = Job(
        external_id=external_id,
        title=title,
        description="负责后端服务开发，要求 python 经验",
        skills=["python"],
        city="杭州",
        apply_url=f"https://j.example/{external_id}",
        status="active",
    )
    session.add(job)
    session.commit()
    return job


class _FakeProvider:
    """确定性假嵌入：维度与配置一致（过门禁），内容全同——只为打通分流路径。"""

    dimension = settings.embeddings_dim

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1] * self.dimension for _ in texts]


# ---------- provider 门禁 ----------


def test_provider_none_by_default():
    assert get_provider() is None


def test_provider_unknown_name_fails_loud(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_provider", "openai")
    with pytest.raises(ValueError, match="未知 EMBEDDINGS_PROVIDER"):
        get_provider()


def test_provider_lazy_no_torch_needed(monkeypatch):
    """构造 provider 不触发 import/下载；embed 时缺依赖要报可读的安装提示。

    用 sys.modules[name]=None 模拟未装（ImportError），与本机是否装过无关。
    """
    import sys

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    provider = SentenceTransformerProvider("BAAI/bge-m3", "cpu", dimension=1024)
    assert provider._model is None  # 懒加载：构造零副作用
    with pytest.raises(ImportError, match="sentence-transformers"):
        provider.embed(["后端开发"])


def test_provider_embed_dimension_gate():
    """模型产出维度与 EMBEDDINGS_DIM 不符必须报错（换模型没同步改配置要早暴露）。"""
    provider = SentenceTransformerProvider("BAAI/bge-m3", "cpu", dimension=1024)

    class _WrongDimModel:
        def encode(self, texts, **kwargs):
            return [[0.1] * 384 for _ in texts]  # 模型实际 384 维，配置写 1024

    provider._model = _WrongDimModel()
    with pytest.raises(ValueError, match="维度不符"):
        provider.embed(["算法工程师"])


# ---------- 向量协议与门禁 ----------


def test_to_pg_vector_format_and_precision(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_dim", 2)  # 格式测试只看文本协议，维度随测试
    assert vector_store.to_pg_vector([0.123456789, 1.0]) == "[0.123457,1.000000]"


def test_to_pg_vector_dimension_gate(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_dim", 4)
    with pytest.raises(ValueError, match="向量维度"):
        vector_store.to_pg_vector([0.1, 0.2])  # 2 维 != 4
    monkeypatch.setattr(settings, "embeddings_dim", 2)
    assert vector_store.to_pg_vector([0.1, 0.2]) == "[0.100000,0.200000]"


def test_sqlite_defenses(session):
    """SQLite 上全部安全退 false/空：单测零依赖原则。"""
    assert vector_store._is_pg(session) is False
    assert vector_store.infra_ready(session) is False
    assert vector_store.ensure_column(session) is False
    assert vector_store.missing_vector_ids(session, [1, 2]) == []
    assert vector_store.upsert_job_vectors(session, [(1, [0.1] * settings.embeddings_dim)]) == 0
    assert vector_store.search_similar(session, [0.1] * settings.embeddings_dim) == []


# ---------- match 分流：默认 TF-IDF 不变，向量模式接管 ----------


def test_refresh_matches_default_tfidf_note(session):
    """provider=none（默认）：explain 语义项仍是 tfidf_cosine，vec 分为 TF-IDF 余弦。"""
    user = _make_user(session, "tfidf@example.com")
    resume = _make_resume(session, user)
    _make_job(session, "vec-1", "后端开发工程师")
    refresh_matches(session, resume)

    row = session.query(MatchScore).filter_by(resume_id=resume.id).first()
    assert row.vec_score is not None
    notes = [e.get("note") for e in row.explain if e.get("key") == "semantic"]
    assert notes == ["tfidf_cosine"]


def test_refresh_matches_vector_mode_takeover(session, monkeypatch):
    """向量模式：vec 分来自 `<=>` 检索结果，explain 标 embedding_cosine（口径可审计）。"""
    user = _make_user(session, "vecmode@example.com")
    resume = _make_resume(session, user)
    job_hit = _make_job(session, "vec-hit", "Python Backend Engineer")
    _make_job(session, "vec-other", "Product Manager")

    monkeypatch.setattr(vector_store, "infra_ready", lambda s: True)
    monkeypatch.setattr(vector_store, "missing_vector_ids", lambda s, ids=None: [])
    monkeypatch.setattr(
        vector_store,
        "search_similar",
        lambda s, q, limit=None, only_active=True: [
            (job_hit.id, 0.9),  # 其余职位 0.1，命中职位 0.9
        ]
        + [(j.id, 0.1) for j in session.query(Job).all() if j.id != job_hit.id],
    )
    fake = _FakeProvider()
    monkeypatch.setattr("app.services.embeddings.get_provider", lambda: fake)

    refresh_matches(session, resume)
    rows = session.query(MatchScore).filter_by(resume_id=resume.id).all()
    by_job = {r.job_id: r for r in rows}
    assert float(by_job[job_hit.id].vec_score) == pytest.approx(0.9)
    note = next(
        e["note"] for e in by_job[job_hit.id].explain if e.get("key") == "semantic"
    )
    assert note == "embedding_cosine"
    assert fake.calls and len(fake.calls[-1]) == 1  # 最后一批是简历查询文本单条
    assert "后端开发" in fake.calls[-1][0] and "三年经验" in fake.calls[-1][0]


def test_vector_mode_ensures_missing_vectors(session, monkeypatch):
    """缺向量的职位现场补嵌（机器兜底）：embed 被调 + upsert 收到缺向量职位。"""
    user = _make_user(session, "backfill@example.com")
    resume = _make_resume(session, user)
    job_missing = _make_job(session, "vec-missing", "数据工程师")

    upserted: list[tuple[int, list[float]]] = []
    monkeypatch.setattr(vector_store, "infra_ready", lambda s: True)
    monkeypatch.setattr(
        vector_store, "missing_vector_ids", lambda s, ids=None: [job_missing.id]
    )
    monkeypatch.setattr(
        vector_store,
        "upsert_job_vectors",
        lambda s, pairs: upserted.extend(pairs) or len(pairs),
    )
    monkeypatch.setattr(
        vector_store,
        "search_similar",
        lambda s, q, limit=None, only_active=True: [(j.id, 0.5) for j in session.query(Job).all()],
    )
    fake = _FakeProvider()
    monkeypatch.setattr("app.services.embeddings.get_provider", lambda: fake)

    refresh_matches(session, resume)
    assert [jid for jid, _ in upserted] == [job_missing.id]
    assert len(fake.calls) == 2  # 一批职位文本 + 一次简历查询文本


# ---------- PG 集成（真容器；未设 JDA_TEST_PG_URL 跳过） ----------


@pytest.fixture()
def pg_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(PG_URL)
    with Session(engine) as s:
        yield s
        s.query(Job).filter(Job.external_id.like("pgvec-%")).delete(synchronize_session=False)
        s.commit()


@pytest.mark.skipif(not PG_URL, reason="JDA_TEST_PG_URL 未设置（PG 集成测试需要真容器）")
class TestPgVectorIntegration:
    def test_ensure_column_idempotent(self, pg_session):
        assert vector_store.ensure_column(pg_session) is True
        assert vector_store.ensure_column(pg_session) is True  # 幂等
        assert vector_store.infra_ready(pg_session) is True

    def test_upsert_and_search_order(self, pg_session):
        vector_store.ensure_column(pg_session)
        dim = settings.embeddings_dim
        jobs = [
            Job(external_id="pgvec-a", title="a", apply_url="https://j.example/a", status="active"),
            Job(external_id="pgvec-b", title="b", apply_url="https://j.example/b", status="active"),
            Job(external_id="pgvec-null", title="c", apply_url="https://j.example/c", status="active"),
        ]
        pg_session.add_all(jobs)
        pg_session.commit()
        # a 与查询向量同向（相似 1），b 正交（相似 0），c 不嵌（NULL 不参与）
        va = [1.0] + [0.0] * (dim - 1)
        vb = [0.0, 1.0] + [0.0] * (dim - 2)
        vector_store.upsert_job_vectors(pg_session, [(jobs[0].id, va), (jobs[1].id, vb)])

        results = vector_store.search_similar(pg_session, va)
        sims = dict(results)
        assert set(sims) == {jobs[0].id, jobs[1].id}  # NULL 行被排除
        assert abs(sims[jobs[0].id] - 1.0) < 1e-6
        assert abs(sims[jobs[1].id]) < 1e-6

    def test_missing_vector_ids(self, pg_session):
        vector_store.ensure_column(pg_session)
        job = Job(external_id="pgvec-m", title="m", apply_url="https://j.example/m", status="active")
        pg_session.add(job)
        pg_session.commit()
        assert job.id in vector_store.missing_vector_ids(pg_session, [job.id])
        dim = settings.embeddings_dim
        vector_store.upsert_job_vectors(pg_session, [(job.id, [0.5] * dim)])
        assert job.id not in vector_store.missing_vector_ids(pg_session, [job.id])
