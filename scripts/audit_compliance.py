"""打印采集合规审计表（§14 ③）：逐公司 robots/terms 结论 + 覆盖缺口。

与 GET /api/compliance/audit 同一实现（app.services.compliance.audit_report），
方便在没有前端/API 的机器上出表；`--markdown` 直接输出可粘贴进开发文档 §14 的表格。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe ../scripts/audit_compliance.py [--markdown]

只读数据库、不触发任何外部请求。审计行本身在 app/services/compliance.py 维护。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.db import SessionLocal  # noqa: E402
from app.services.compliance import audit_report, markdown_table, validate_audit_rows  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", action="store_true", help="输出 Markdown 表（默认输出逐行明细）")
    args = parser.parse_args()

    problems = validate_audit_rows()
    if problems:
        for p in problems:
            print(f"[结构错误] {p}", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        report = audit_report(session)

    cov = report["coverage"]
    print(f"库内公司 {cov['companies']} 家，已审计 {cov['audited']} 家（覆盖 {cov['ratio']:.0%}）")
    print(f"在采：{', '.join(report['collecting']) or '无'}")
    print(f"休眠：{', '.join(report['dormant']) or '无'}")
    if report["unreviewed"]:
        print(f"[缺口] 未审计公司需先补审计行再开采集：{', '.join(report['unreviewed'])}")

    if args.markdown:
        print()
        print(markdown_table(report["rows"]))
    else:
        print()
        for r in report["rows"]:
            if not r["audited"]:
                print(f"- {r['slug']}（{r['ats_type']}）：**未审计**")
                continue
            print(f"- {r['slug']}（{r['ats_type']}）｜ {r['decision']} ｜ 复核 {r['reviewed_at']}")
            print(f"    robots[{r['robots']}]：{r['robots_note']}")
            print(f"    terms[{r['terms']}]：{r['terms_note']}")

    for note in report["notes"]:
        print(f"提示：{note}")
    return 1 if report["unreviewed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())