"""生产配置审计脚本（§14 ⑤）：读真实 Settings + git 跟踪状态，跑 prodcheck 并打印结论。

用法（backend/ 下）：
    ../.venv/Scripts/python.exe ../scripts/audit_production_config.py

返回码：0 = 无 fail（可能含 warn）；1 = 有 fail（上线阻断）。
只读：不改配置、不写 .env、不打印密钥原文。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.core.config import settings  # noqa: E402
from app.services.prodcheck import audit_config  # noqa: E402

ENV_EXAMPLE = REPO / "backend" / ".env.example"

LEVEL_TAG = {"fail": "阻断", "warn": "待确认", "info": "提示"}


def _env_tracked() -> bool:
    """backend/.env 是否被 git 跟踪（--error-unmatch 在未跟踪时返回非 0）。

    git 不可用（未装/非仓库）时返回 False——审计不因此中断，只少一项检查。
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "backend/.env"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0


def main() -> int:
    example = ENV_EXAMPLE.read_text(encoding="utf-8") if ENV_EXAMPLE.exists() else ""
    report = audit_config(settings, env_tracked=_env_tracked(), env_example_text=example)

    counts = report["counts"]
    print(f"审计结论：{report['status']}（阻断 {counts['fail']} ｜ 待确认 {counts['warn']}）")
    if report["checks"]:
        for check in report["checks"]:
            print(f"  [{LEVEL_TAG[check['level']]}] {check['message']}")
    else:
        print("  未发现问题。")

    print("\n密钥轮换清单（人工在服务商侧执行：作废旧值 → 签发新值 → 写入生产环境变量）：")
    for item in report["rotation"]:
        print(f"  - {item['field']}：{item['why']}")

    for note in report["notes"]:
        print(f"提示：{note}")

    return 1 if counts["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())