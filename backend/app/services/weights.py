"""匹配权重的采纳与回滚（§12.5 反馈回灌闭环收尾）。

闭环全貌：
    feedback_log（状态机每次变更回灌）
      → insights.build_quality_report / tuning.search_weights（NDCG@k 评估）
      → **本模块：把候选权重视为可版本化的数据并采纳**
      → match_scores 全量重算（`match.refresh_matches_all`）
      → 排序生效；出问题回滚到上一版。

关键取舍：
- **落库而不写 `.env`**：`.env` 是宿主机部署配置（改它要重启、不可审计、不可回滚），
  而权重是**随反馈演进的数据**。每次采纳写一行 `match_weight_versions`，
  生效权重 = 最新一行；表空时回退 `settings.MATCH_W_*`（旧部署行为不变）；
- **调参报告仍只读**：`GET /api/insights/tuning` 只给建议；要生效必须显式 apply
  或打开 `WEIGHTS_AUTO_TUNE`（默认关）——"自动改线上排序"这种事不能默认开；
- **样本与幅度双闸**：自动采纳要求样本 ≥ `weights_auto_tune_min_sample`（默认 10）
  且 NDCG 提升 ≥ `weights_auto_tune_margin`（默认 0.02）——低于这个幅度的"提升"
  多半是噪声，换权重反而让用户困惑；
- **单一口径**：`active_weights()` 是唯一解析入口，打分（match）、质量报告（insights）、
  调参基线（tuning）都从它取，杜绝"看板显示的权重 ≠ 实际排序用的权重"。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import MatchWeightVersion

WEIGHT_KEYS = ("skill", "city", "exp", "role")
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
SUM_TOLERANCE = 1e-3


@dataclass(frozen=True)
class WeightSet:
    """一组生效权重（四分项 + α/β），来源可追溯。"""

    skill: float
    city: float
    exp: float
    role: float
    alpha: float
    source: str = SOURCE_MANUAL

    @property
    def beta(self) -> float:
        return round(1 - self.alpha, 4)

    def as_dict(self) -> dict:
        return {
            "skill": self.skill,
            "city": self.city,
            "exp": self.exp,
            "role": self.role,
            "alpha": self.alpha,
            "beta": self.beta,
            "source": self.source,
        }

    @classmethod
    def from_settings(cls) -> "WeightSet":
        return cls(
            skill=settings.match_w_skill,
            city=settings.match_w_city,
            exp=settings.match_w_exp,
            role=settings.match_w_role,
            alpha=settings.match_alpha,
            source="settings",
        )


def validate(weights: dict) -> WeightSet:
    """校验并归一化一组权重；任何不自洽的组合一律抛 ValueError（接口层转 400）。"""
    if not isinstance(weights, dict):
        raise ValueError("weights 必须是对象")
    values: dict[str, float] = {}
    for key in WEIGHT_KEYS:
        raw = weights.get(key)
        if raw is None:
            raise ValueError(f"缺少权重项 {key}")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"权重 {key} 不是数字") from None
        if not 0 <= value <= 1:
            raise ValueError(f"权重 {key} 必须在 [0,1] 之间，收到 {value}")
        values[key] = round(value, 4)

    alpha_raw = weights.get("alpha")
    if alpha_raw is None:
        raise ValueError("缺少 alpha")
    try:
        alpha = float(alpha_raw)
    except (TypeError, ValueError):
        raise ValueError("alpha 不是数字") from None
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha 必须在 (0,1] 之间，收到 {alpha}")

    total = sum(values.values())
    if abs(total - 1.0) > SUM_TOLERANCE:
        raise ValueError(f"四分项权重之和必须为 1，当前 {round(total, 4)}（容差 {SUM_TOLERANCE}）")

    beta = weights.get("beta")
    if beta is not None and abs(float(beta) - (1 - alpha)) > SUM_TOLERANCE:
        raise ValueError(f"beta 必须等于 1-alpha（{round(1 - alpha, 4)}），收到 {beta}")

    # source 是来源标注（settings/manual/auto），透传即可，不参与数学校验
    source = str(weights.get("source") or SOURCE_MANUAL)
    return WeightSet(alpha=round(alpha, 4), source=source, **values)


def _row_to_dict(row: MatchWeightVersion) -> dict:
    weights = dict(row.weights or {})
    return {
        "id": row.id,
        "weights": weights,
        "source": row.source,
        "note": row.note,
        "sample_size": row.sample_size,
        "baseline_ndcg": row.baseline_ndcg,
        "new_ndcg": row.new_ndcg,
        "rematched": row.rematched,
        "created_at": str(row.created_at) if row.created_at else None,
    }


def current_version(session: Session) -> MatchWeightVersion | None:
    """最新一次采纳（无记录返回 None）。"""
    return session.execute(
        select(MatchWeightVersion).order_by(MatchWeightVersion.id.desc()).limit(1)
    ).scalars().first()


def active_weights(session: Session | None = None) -> WeightSet:
    """生效权重：有采纳记录取最新一行，否则回退设置（`session=None` 只用于无库场景）。"""
    if session is None:
        return WeightSet.from_settings()
    row = current_version(session)
    if row is None:
        return WeightSet.from_settings()
    try:
        return validate({**row.weights, "source": row.source})
    except ValueError:
        # 库里的行不自洽（人为改库）——不让整站打分挂掉，回退设置并可见于历史
        return WeightSet.from_settings()


def history(session: Session, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(MatchWeightVersion).order_by(MatchWeightVersion.id.desc()).limit(max(1, min(limit, 100)))
    ).scalars().all()
    return [_row_to_dict(r) for r in rows]


def apply_weights(
    session: Session,
    weights: dict,
    *,
    source: str = SOURCE_MANUAL,
    note: str | None = None,
    sample_size: int | None = None,
    baseline_ndcg: float | None = None,
    new_ndcg: float | None = None,
    rematch: bool = True,
) -> dict:
    """采纳一组权重：写入新版本行 → （默认）全量重算 match_scores。

    返回 {"applied": True, "version": {...}, "active": {...}, "rematched": N}；
    非法权重抛 ValueError（不落库、不重算）。
    """
    parsed = validate(weights)
    row = MatchWeightVersion(
        weights={k: getattr(parsed, k) for k in WEIGHT_KEYS} | {"alpha": parsed.alpha, "beta": parsed.beta},
        source=source,
        note=note,
        sample_size=sample_size,
        baseline_ndcg=baseline_ndcg,
        new_ndcg=new_ndcg,
    )
    session.add(row)
    session.commit()

    rematched = 0
    if rematch:
        from app.pipelines.match import refresh_matches_all  # 局部导入避开模块级循环

        rematched = refresh_matches_all(session)
        row.rematched = rematched
        session.commit()
    return {
        "applied": True,
        "version": _row_to_dict(row),
        "active": {**parsed.as_dict(), "source": source},
        "rematched": rematched,
    }


def rollback(session: Session, *, rematch: bool = True) -> dict:
    """回滚到上一版（删除最新采纳行）；没有采纳记录时抛 ValueError。"""
    row = current_version(session)
    if row is None:
        raise ValueError("没有可回滚的采纳记录（当前用的是 .env 设置权重）")
    removed = _row_to_dict(row)
    session.execute(delete(MatchWeightVersion).where(MatchWeightVersion.id == row.id))
    session.commit()

    active = active_weights(session)
    rematched = 0
    if rematch:
        from app.pipelines.match import refresh_matches_all

        rematched = refresh_matches_all(session)
    return {"rolled_back": True, "removed": removed, "active": active.as_dict(), "rematched": rematched}


def auto_tune(
    session: Session,
    *,
    k: int = 10,
    step: float = 0.1,
    top: int = 5,
    min_sample: int | None = None,
    margin: float | None = None,
    min_resumes: int | None = None,
    apply: bool = False,
    rematch: bool = True,
) -> dict:
    """跑一次"反馈 → 调参 → （可选）采纳"的闭环；默认只评估不生效。

    门槛：样本 < `min_sample` → insufficient_sample；网格内无更优 → no_gain；
    提升低于 `margin` → gain_below_margin（不"为了闭环而闭环"地换参数，
    换权重的收益必须盖过噪声）。
    `min_resumes`：评估网格所需的最小有反馈简历数（默认 3；调低仅用于小样本试跑/测试）。
    """
    from app.services.tuning import search_weights

    min_sample = settings.weights_auto_tune_min_sample if min_sample is None else min_sample
    margin = settings.weights_auto_tune_margin if margin is None else margin
    min_resumes = 3 if min_resumes is None else min_resumes

    report = search_weights(session, k=k, step=step, top=top, min_resumes=min_resumes)
    result: dict = {
        "evaluated": True,
        "k": k,
        "step": step,
        "samples": report["samples"],
        "baseline": report["baseline"],
        "best": report["best"],
        "min_sample": min_sample,
        "margin": margin,
        "applied": False,
        "reason": None,
        "notes": list(report["notes"]),
    }
    resumes = int((report["samples"] or {}).get("resumes") or 0)
    if resumes < min_sample:
        result["reason"] = "insufficient_sample"
        return result
    if not report["best"]:
        result["reason"] = "no_gain"
        return result

    gain = float(report["best"].get("gain") or 0)
    result["gain"] = round(gain, 4)
    if gain < margin:
        result["reason"] = "gain_below_margin"
        return result

    if not apply:
        result["reason"] = "dry_run"
        result["notes"].append(f"建议采纳（增益 {round(gain, 4)} ≥ 门槛 {margin}）；dry_run 未生效")
        return result

    applied = apply_weights(
        session,
        report["best"]["weights"],
        source=SOURCE_AUTO,
        note=f"auto_tune: NDCG@{k} {report['baseline']['ndcg_at_k']} → {report['best']['ndcg_at_k']}",
        sample_size=resumes,
        baseline_ndcg=report["baseline"]["ndcg_at_k"],
        new_ndcg=report["best"]["ndcg_at_k"],
        rematch=rematch,
    )
    result.update(applied=True, reason="applied", version=applied["version"], rematched=applied["rematched"])
    return result

