"""Redis 令牌桶限流 + 别名缓存测试：fake redis 注入，不触真实 Redis。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import app.services.collect as collect_mod
import app.services.ratelimit as rl_mod
from app.models import Company, FetchLog


class FakeRedis:
    """eval 返回固定值；get/setex 走内存字典。"""

    def __init__(self, eval_result=1):
        self.eval_result = eval_result
        self.eval_calls = []
        self.store = {}

    def eval(self, script, numkeys, key, *args):
        self.eval_calls.append((key, args))
        return self.eval_result

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


class BoomRedis:
    def eval(self, *args, **kwargs):
        raise ConnectionError("redis down")


# ---------- 令牌桶 acquire ----------


def test_acquire_allowed(monkeypatch):
    fake = FakeRedis(eval_result=1)
    monkeypatch.setattr(rl_mod, "get_redis", lambda: fake)
    assert rl_mod.acquire("acme", 1, 360) is True
    key, args = fake.eval_calls[0]
    assert key == "ratelimit:acme"


def test_acquire_blocked(monkeypatch):
    fake = FakeRedis(eval_result=0)
    monkeypatch.setattr(rl_mod, "get_redis", lambda: fake)
    assert rl_mod.acquire("acme", 1, 360) is False


def test_acquire_redis_down_returns_none(monkeypatch):
    monkeypatch.setattr(rl_mod, "get_redis", lambda: BoomRedis())
    assert rl_mod.acquire("acme", 1, 360) is None


def test_acquire_no_redis_returns_none(monkeypatch):
    monkeypatch.setattr(rl_mod, "get_redis", lambda: None)
    assert rl_mod.acquire("acme", 1, 360) is None


# ---------- 采集侧令牌桶第二层 ----------


def test_collect_token_bucket_skip(session, monkeypatch):
    from app.adapters.base import NormalizedJob
    from app.services.collect import collect_company

    company = Company(slug="acme", name="Acme", ats_type="fake", fetch_policy={"interval_min": 60})
    session.add(company)
    session.commit()

    jobs = [NormalizedJob(external_id="J1", title="A", apply_url="https://x.com/1", source="fake")]
    monkeypatch.setattr(collect_mod, "get_adapter", lambda t, client=None: type("A", (), {"normalize_all": lambda self, c: jobs})())
    monkeypatch.setattr(collect_mod, "acquire", lambda key, rate=1, window=360: False)

    result = collect_company(session, company, now=datetime(2026, 9, 19, 12, 0, 0))
    assert result == {"company": "acme", "status": "skipped", "reason": "token_bucket"}
    log = session.query(FetchLog).one()
    assert log.status == "rate_limited"


# ---------- 别名映射 Redis 缓存 ----------


def test_alias_map_cache_hit(session, monkeypatch):
    fake = FakeRedis()
    fake.store["cache:alias_map"] = json.dumps({"py": "python"})
    monkeypatch.setattr(collect_mod, "get_redis", lambda: fake)

    mapping = collect_mod.build_alias_map(session)
    assert mapping == {"py": "python"}  # 缓存命中，不查 DB


def test_alias_map_cache_miss_builds_and_stores(session, monkeypatch):
    from app.models import SkillTag

    session.add(SkillTag(canonical="rust", category="skill", aliases=["rust-lang"]))
    session.commit()

    fake = FakeRedis()
    monkeypatch.setattr(collect_mod, "get_redis", lambda: fake)

    mapping = collect_mod.build_alias_map(session)
    assert mapping["k8s"] == "kubernetes"  # 内置别名
    assert mapping["rust-lang"] == "rust"  # skill_tags 字典

    cached = json.loads(fake.store["cache:alias_map"])
    assert cached == mapping  # 已写缓存


def test_alias_map_cache_disabled(session, monkeypatch):
    """use_cache=False 时不碰 Redis（直查 DB，行为与旧版一致）。"""
    from app.models import SkillTag

    session.add(SkillTag(canonical="rust", category="skill", aliases=[]))
    session.commit()

    called = {"n": 0}

    def _guard():
        called["n"] += 1
        return None

    monkeypatch.setattr(collect_mod, "get_redis", _guard)
    mapping = collect_mod.build_alias_map(session, use_cache=False)
    assert mapping["rust"] == "rust"
    assert called["n"] == 0  # use_cache=False 短路，get_redis 不被调用
