"""共享测试夹具：SQLite 内存库，不触网、不触 Redis。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部模型
from app.core import redis_utils
from app.core.db import Base


@pytest.fixture(autouse=True)
def _seal_redis(monkeypatch):
    """测试密封：本机 .env 可能配了 redis_url（生产化后常驻），
    强制 get_redis 返回 None，令牌桶/别名缓存全部走降级路径。"""

    def _none():
        return None

    monkeypatch.setattr(redis_utils, "get_redis", _none)


@pytest.fixture()
def client(session):
    """覆盖依赖的 TestClient，使用内存库。"""

    from app.main import app, get_session

    def override_get_session():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    engine.dispose()
