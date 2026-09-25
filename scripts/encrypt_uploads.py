"""简历原件静态加密的存量处理与追溯工具（§10 个人信息）。

两件事，都在本机跑、都不打印文件内容：
1. **存量加密**（默认）：把 `uploads_dir` 下已存在的明文原件就地加密（幂等——已是密文跳过）；
2. **解密追溯**（`--decrypt <文件>`）：把某份密文解出来到 `.plain`（用于人工追溯/重解析）。
   解密产物落在同一目录，属"临时明文"，用完请自行删除。

用法（backend/ 下，或任意目录直接用路径）：
    ../.venv/Scripts/python.exe ../scripts/encrypt_uploads.py [--dry-run]
    ../.venv/Scripts/python.exe ../scripts/encrypt_uploads.py --decrypt uploads/resume_1_1700000000.pdf

前置：`.env` 里配好 `UPLOADS_KEY`（未配置则本脚本无事可做，直接报错退出）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.config import settings  # noqa: E402
from app.services import crypto  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    parser.add_argument("--decrypt", metavar="FILE", help="解密单个文件到 <FILE>.plain")
    parser.add_argument("--out", help="与 --decrypt 配合，指定解密产物路径")
    args = parser.parse_args()

    if not crypto.enabled():
        print("UPLOADS_KEY 未配置：静态加密未启用，无需处理（先配密钥再跑）")
        return 2

    if args.decrypt:
        try:
            dest = crypto.decrypt_file(args.decrypt, args.out)
        except FileNotFoundError:
            print(f"文件不存在: {args.decrypt}")
            return 1
        except crypto.CryptoError as exc:
            print(f"解密失败: {exc}")
            return 1
        print(f"已解密到: {dest}（用完请删除）")
        return 0

    root = Path(settings.uploads_dir)
    if not root.is_dir():
        print(f"上传目录不存在: {root}")
        return 0

    plain: list[Path] = []
    already: int = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if crypto.is_encrypted(path.read_bytes()):
            already += 1
        else:
            plain.append(path)

    print(f"目录: {root}")
    print(f"已是密文: {already}｜待加密: {len(plain)}")
    if args.dry_run:
        for path in plain:
            print(f"  [dry-run] {path.name}")
        return 0

    changed = 0
    for path in plain:
        try:
            if crypto.encrypt_file(path):
                changed += 1
        except OSError as exc:
            print(f"  失败 {path.name}: {exc}")
    print(f"已加密: {changed}｜跳过: {len(plain) - changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
