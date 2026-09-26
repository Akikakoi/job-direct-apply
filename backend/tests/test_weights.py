"""§12.5 反馈回灌闭环用例：权重版本的采纳 / 回滚 / 自动调参 / 打分同口径。

闭环的核心保证：
1. **生效权重只有一个来源**（`active_weights`）：打分、质量报告、调参基线三处读到的
   必须是同一组数——否则"看板显示 A、排序用 B"这种漂移没法被任何人发现；
2. **采纳必须可回滚**（历史即审计轨迹）；
3. **自动采纳有双门槛**（样本量 + NDCG 增益），达不到就如实说不换参数；
4. 权重不自洽的组合（和 ≠1、越界、beta ≠ 1-α）一律拒绝，绝不带着脏权重重算全库。
"""

from __future__ import annotations

import importlib

import pytest

from app.core.config import settings
from app.models import Application, FeedbackLog, Job, MatchScore, Resume, User
from app.pipelines.match import refresh_matches_all
from app.services.weights import (
    WeightSet,
    active_weights,
    apply_weights,
    auto_tune,
    history,
    rollback,
    validate,
)


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


def _make_resume(session, user: User, skills=("python",), cities=("杭州",)) -> Resume:
    resume = Resume(
        user_id=user.id,
        raw_text=" ".join(skills),
        profile={"skills": list(skills), "cities": list(cities), "target_role": "后端"},
        is_active=True,
    )
    session.add(resume)
    session.commit()
    return resume


def _make_job(session, external_id: str, title: str, skills, city: str | None) -> Job:
    job = Job(external_id=external_id, title=title, skills=list(skills), city=city, apply_url=f"https://j.example/{external_id}", status="active")
    session.add(job)
    session.commit()
    return job


def _make_application(session, user: User, resume: Resume, job: Job, outcome: str | None = None) -> Application:
    app_row = Application(
        user_id=user.id,
        resume_id=resume.id,
        job_id=job.id,
        status="submitted",
        apply_url=job.apply_url,
        authorized=True,
    )
    session.add(app_row)
    session.commit()
    if outcome:
        session.add(FeedbackLog(application_id=app_row.id, outcome=outcome))
        session.commit()
    return app_row


def _scenario(session, n_resumes: int = 1):
    """最小闭环场景：技能匹配的职位被拒、城市匹配的职位拿 offer。

    基线权重（skill=0.5 > city=0.2）会把 A 排在 B 前面（NDCG=0），
    而城市主导的权重（city=1.0）能排对（NDCG=1）——增益 1.0，远超任何门槛。
    """
    job_a = _make_job(session, "ext-a", "Python 工程师", ["python"], "北京")
    job_b = _make_job(session, "ext-b", "销售经理", ["sales"], "杭州")
    for i in range(n_resumes):
        user = _make_user(session, f"u{i}@example.com")
        resume = _make_resume(session, user)
        _make_application(session, user, resume, job_a, outcome="rejected")
        _make_application(session, user, resume, job_b, outcome="offer")
    return job_a, job_b


# ---------- 校验 ----------


def test_validate_accepts_consistent_weights():
    w = validate({"skill": 0.5, "city": 0.2, "exp": 0.15, "role": 0.15, "alpha": 0.75})
    assert w.skill == 0.5 and w.alpha == 0.75 and w.beta == 0.25


def test_validate_rejects_inconsistent_weights():
    bad_cases = [
        {},  # 全缺
        {"skill": 0.5, "city": 0.2, "exp": 0.15},  # 缺 role
        {"skill": 0.5, "city": 0.2, "exp": 0.15, "role": 0.15},  # 缺 alpha
        {"skill": 0.6, "city": 0.2, "exp": 0.15, "role": 0.15, "alpha": 0.75},  # 和 ≠ 1
        {"skill": 1.2, "city": 0, "exp": 0, "role": 0, "alpha": 0.75},  # 越界
        {"skill": 0.5, "city": 0.2, "exp": 0.15, "role": 0.15, "alpha": 1.5},  # alpha 越界
        {"skill": 0.5, "city": 0.2, "exp": 0.15, "role": 0.15, "alpha": 0.75, "beta": 0.4},  # beta 不自洽
        {"skill": "abc", "city": 0.2, "exp": 0.15, "role": 0.15, "alpha": 0.75},  # 非数字
    ]
    for case in bad_cases:
        with pytest.raises(ValueError):
            validate(case)


def test_active_weights_falls_back_to_settings_when_no_versions(session):
    active = active_weights(session)
    assert active.source == "settings"
    assert active.skill == settings.match_w_skill and active.alpha == settings.match_alpha
    # 无 session（无库场景）也回退设置
    assert active_weights(None).source == "settings"


# ---------- 采纳 / 回滚 ----------


def test_apply_persists_version_and_rematch_uses_new_weights(session):
    from sqlalchemy import select as _select

    job_a = _make_job(session, "ext-a", "Python 工程师", ["python"], "北京")
    job_b = _make_job(session, "ext-b", "销售经理", ["sales"], "杭州")
    user = _make_user(session, "u@example.com")
    resume = _make_resume(session, user)
    refresh_matches_all(session)
    before = session.execute(
        _select(MatchScore).where(MatchScore.job_id == job_b.id)
    ).scalars().one()
    assert float(before.rule_score) == 0.35  # 基线（skill=0.5）：B 只有城市分 0.2 + 经验中性 0.15
    # final = alpha*rule + beta*vec（vec=0 → 0.75*0.35 = 0.2625）。列宽 Numeric(6,4) 后
    # 四位小数不再被截断（旧 Numeric(5,2) 下这里只能读到 0.26，正是并列的元凶）。
    assert float(before.final_score) == 0.2625

    result = apply_weights(
        session,
        {"skill": 0.0, "city": 1.0, "exp": 0.0, "role": 0.0, "alpha": 0.75},
        source="manual",
        note="测试采纳",
    )
    assert result["applied"] is True and result["rematched"] > 0

    after_b = session.execute(
        _select(MatchScore).where(MatchScore.job_id == job_b.id)
    ).scalars().one()
    assert float(after_b.rule_score) == 1.0  # 城市主导权重下 B（杭州匹配）规则分满分
    assert float(after_b.final_score) == 0.75  # final = alpha*rule + beta*vec（B 无文本重叠 → vec=0）

    # 生效权重与历史
    active = active_weights(session)
    assert active.city == 1.0 and active.source == "manual"
    hist = history(session)
    assert hist[0]["note"] == "测试采纳" and hist[0]["source"] == "manual"


def test_rollback_restores_previous_version(session):
    apply_weights(session, {"skill": 0.4, "city": 0.3, "exp": 0.15, "role": 0.15, "alpha": 0.8}, rematch=False)
    apply_weights(session, {"skill": 0.1, "city": 0.6, "exp": 0.15, "role": 0.15, "alpha": 0.7}, rematch=False)
    assert active_weights(session).city == 0.6

    result = rollback(session, rematch=False)
    assert result["rolled_back"] is True and result["removed"]["weights"]["city"] == 0.6
    assert active_weights(session).city == 0.3  # 回到上一版

    rollback(session, rematch=False)
    assert active_weights(session).source == "settings"  # 版本表清空 → 回退 .env
    with pytest.raises(ValueError):
        rollback(session)  # 没有可回滚的了


# ---------- 自动调参（反馈 → 评估 → 采纳） ----------


def test_auto_tune_reports_insufficient_sample(session):
    result = auto_tune(session, min_sample=10, min_resumes=1)
    assert result["applied"] is False and result["reason"] == "insufficient_sample"


def test_auto_tune_applies_when_gain_beats_margin(session):
    _scenario(session, n_resumes=1)
    result = auto_tune(session, min_sample=1, margin=0.1, min_resumes=1, apply=True)
    assert result["applied"] is True and result["reason"] == "applied"
    assert result["gain"] >= 0.1
    assert result["version"]["source"] == "auto"
    assert result["rematched"] > 0
    # 采纳即生效：打分/报告/调参三处同口径（新版本 NDCG=1，基线 0）
    active = active_weights(session)
    assert active.source == "auto"
    hist = history(session)[0]
    assert hist["new_ndcg"] == 1.0 and hist["new_ndcg"] > hist["baseline_ndcg"]  # 新权重把 offer 职位排到第一
    from app.services.insights import weights_snapshot

    assert weights_snapshot(session)["source"] == "auto"
    assert weights_snapshot(session) == active.as_dict()


def test_auto_tune_skips_when_gain_below_margin(session):
    _scenario(session, n_resumes=1)
    result = auto_tune(session, min_sample=1, margin=0.99, min_resumes=1, apply=True)
    assert result["applied"] is False and result["reason"] == "gain_below_margin"
    assert active_weights(session).source == "settings"  # 没动线上


def test_auto_tune_dry_run_never_applies(session):
    _scenario(session, n_resumes=1)
    result = auto_tune(session, min_sample=1, margin=0.1, min_resumes=1, apply=False)
    assert result["applied"] is False and result["reason"] == "dry_run"
    assert "dry_run 未生效" in " ".join(result["notes"])


# ---------- 接口 ----------


def test_api_weight_lifecycle(client, session):
    # 默认回退设置
    listed = client.get("/api/match-weights").json()["data"]
    assert listed["source"] == "settings" and listed["history"] == []

    ok = client.post(
        "/api/match-weights/apply",
        json={"weights": {"skill": 0.4, "city": 0.3, "exp": 0.15, "role": 0.15, "alpha": 0.8}, "note": "首轮采纳"},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["rematched"] >= 0

    bad = client.post(
        "/api/match-weights/apply",
        json={"weights": {"skill": 0.9, "city": 0.3, "exp": 0.15, "role": 0.15, "alpha": 0.8}},
    )
    assert bad.status_code == 400  # 和 ≠ 1

    listed = client.get("/api/match-weights").json()["data"]
    assert listed["source"] == "manual" and len(listed["history"]) == 1

    rb = client.post("/api/match-weights/rollback")
    assert rb.status_code == 200
    assert client.get("/api/match-weights").json()["data"]["source"] == "settings"


def test_api_auto_tune_dry_run_and_step_bounds(client, session):
    resp = client.post("/api/match-weights/auto-tune", json={"step": 0.9})
    assert resp.status_code == 422

    resp = client.post("/api/match-weights/auto-tune", json={"min_sample": 5})
    assert resp.status_code == 200
    assert resp.json()["data"]["applied"] is False  # 空库样本不足，且默认 dry-run


def test_api_weight_admin_gate(client, session, monkeypatch):
    """管理动作的鉴权口径与公司映射一致：开关关=旧行为，开=必须 admin。"""
    body = {"weights": {"skill": 0.5, "city": 0.2, "exp": 0.15, "role": 0.15, "alpha": 0.75}}
    assert client.post("/api/match-weights/apply", json=body).status_code == 200

    monkeypatch.setattr(settings, "auth_required", True)
    assert client.post("/api/match-weights/apply", json=body).status_code == 401
    from app.services.auth import create_token, hash_password

    user = User(email="u@example.com", password_hash=hash_password("secret123"), role="user")
    admin = User(email="boss@example.com", password_hash=hash_password("secret123"), role="admin")
    session.add_all([user, admin])
    session.commit()
    assert client.post("/api/match-weights/apply", json=body, headers={"Authorization": f"Bearer {create_token(user.id)}"}).status_code == 403
    assert client.post("/api/match-weights/apply", json=body, headers={"Authorization": f"Bearer {create_token(admin.id, 'admin')}"}).status_code == 200


# ---------- 定时任务 ----------


def test_beat_auto_tune_entry_gated_by_settings(monkeypatch):
    """beat 条目随 WEIGHTS_AUTO_TUNE 注册/注销（任务本身恒可被调用）。

    必须显式 monkeypatch 再 reload：本用例**不依赖开发者本机 `.env`**——生产 `.env`
    可能已打开该开关（第二十六轮起），读本机配置会让断言随机器而变。
    """
    from app.workers import celery_app as mod

    original = settings.weights_auto_tune

    def _reload_with(flag: bool):
        monkeypatch.setattr(settings, "weights_auto_tune", flag)
        return importlib.reload(mod)

    try:
        off = _reload_with(False)
        assert "weights-auto-tune-weekly" not in off.celery_app.conf.beat_schedule

        reloaded = _reload_with(True)
        assert reloaded.celery_app.conf.beat_schedule["weights-auto-tune-weekly"]["task"] == (
            "app.workers.celery_app.auto_tune_weights_task"
        )
    finally:
        # 按"本机当前配置"还原注册状态，避免把临时开关泄漏给后续用例
        settings.weights_auto_tune = original
        importlib.reload(mod)


def test_auto_tune_task_skips_when_disabled(session, monkeypatch):
    from app.workers.celery_app import auto_tune_weights_task

    monkeypatch.setattr(settings, "weights_auto_tune", False)
    monkeypatch.setattr("app.core.db.SessionLocal", lambda: session)
    assert auto_tune_weights_task.run() == {"skipped": True, "reason": "weights_auto_tune_disabled"}

    monkeypatch.setattr(settings, "weights_auto_tune", True)
    result = auto_tune_weights_task.run()  # 空库 → 样本不足，不采纳
    assert result["applied"] is False and result["reason"] == "insufficient_sample"


# ---------- WeightSet 纯函数 ----------


def test_weightset_beta_and_settings_roundtrip():
    w = WeightSet.from_settings()
    assert abs(w.alpha + w.beta - 1) < 1e-9
    parsed = validate(w.as_dict())
    # source 不参与校验（它记录的是"这组数从哪来"，不是数学属性）
    strip = lambda d: {k: v for k, v in d.items() if k != "source"}
    assert strip(parsed.as_dict()) == strip(w.as_dict())
