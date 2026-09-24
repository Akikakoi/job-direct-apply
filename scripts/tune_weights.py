"""离线权重调参（§12.7 #3）：跑网格搜索并打印 NDCG@k 最优建议。

与 GET /api/insights/tuning 同一实现（app.services.tuning.search_weights），
方便在没有前端/API 的机器上直接出结论。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe ../scripts/tune_weights.py [--step 0.1] [--k 10] [--top 5]

只读数据库、打印建议，**不写 .env、不改配置**；采纳需人工确认后改
MATCH_W_SKILL/CITY/EXP/ROLE 与 MATCH_ALPHA，再重算 match_scores。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.db import SessionLocal  # noqa: E402
from app.services.tuning import search_weights  # noqa: E402

TAG = {"skill": "技能", "city": "城市", "exp": "经验", "role": "岗位"}


def _fmt(w: dict) -> str:
    body = "  ".join(f"{TAG[k]}={w[k]}" for k in TAG)
    return f"{body}  α={w['alpha']}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", type=float, default=0.1, help="权重网格粒度（0.05~0.5）")
    parser.add_argument("--k", type=int, default=10, help="NDCG@k 的 k")
    parser.add_argument("--top", type=int, default=5, help="打印前 N 个候选")
    args = parser.parse_args()

    if not 0.05 <= args.step <= 0.5:
        print(f"step 需在 [0.05, 0.5]，当前 {args.step}", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        report = search_weights(session, k=args.k, step=args.step, top=args.top)

    s = report["samples"]
    print(f"样本：{s['resumes']} 份有反馈简历 × {s['jobs']} 个 active 职位")
    base = report["baseline"]
    print(f"基线（当前配置）NDCG@{args.k} = {base['ndcg_at_k']}  {_fmt(base['weights'])}")

    if report["grid"]["combos"]:
        print(f"\n网格：step={report['grid']['step']}，评估组合 {report['grid']['combos']} 个")
        print(f"候选 top{len(report['candidates'])}：")
        for i, c in enumerate(report["candidates"], 1):
            print(f"  {i}. NDCG@{args.k}={c['ndcg_at_k']}  {_fmt(c['weights'])}")

    best = report["best"]
    if best:
        print(f"\n建议：NDCG@{args.k} {base['ndcg_at_k']} → {best['ndcg_at_k']}（+{best['gain']}）")
        print("把以下内容写入 backend/.env 后重启，并重算 match_scores：\n")
        print(best["env_snippet"])
    else:
        print("\n结论：维持现有权重。")

    for note in report["notes"]:
        print(f"提示：{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())