"""导出法务送审稿（§14 ①）：协议/隐私政策中英逐条对照 + 结构校验 + 签署状态。

正文单一事实源是 `frontend/app/legal/legal-text.json`（`/legal/*` 页面渲染的就是同一份字符串），
本脚本**不复制正文**，只做三件事：

① 校验（结构问题 → exit 2）：中英小节 id 与段落结构一一对应、强制披露三节中英齐备、
   英文不残留中文、正文无 TODO/待补 占位、效力声明与 `meta.authoritative_lang` 口径一致、
   前端版本号与后端 `legal.POLICY_VERSION` 一致；
② 出稿（`--lang zh|en|both`，`--out` 落盘，默认 stdout）：可直接发法务审阅的对照稿；
③ 报签署状态：`meta.legal_review` 的签署版本/复核人/复核日齐备且版本未过期才算闭环，
   否则 exit 1——**签署属"非代码"事项，不阻断代码合入，但要让脚本一直说话**（同 audit_compliance 口径）。

用法（仓库根目录）：
    .venv/Scripts/python.exe scripts/export_legal_text.py [--lang both] [--out 送审稿.txt]

只读本地文件、不触网、不连库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.services.legal import (  # noqa: E402
    POLICY_EFFECTIVE_DATE,
    POLICY_VERSION,
    REQUIRED_SECTION_IDS,
)

TEXT_FILE = REPO / "frontend" / "app" / "legal" / "legal-text.json"
SHELL_FILE = REPO / "frontend" / "app" / "legal" / "legal-doc.js"

DOC_LABELS = {"terms": ("用户协议", "Terms of Service"), "privacy": ("隐私政策", "Privacy Policy")}
LANGS = ("zh", "en")
CJK = re.compile(r"[\u4e00-\u9fff]")
# 占位词：正文里出现即说明还是草稿，不能送审
PLACEHOLDERS = ("TODO", "TBD", "待补", "待定", "XXX", "Lorem ipsum")
# 每节最少字数：英文明显短于中文属正常，但整节空着/一句话占位要拦住
MIN_SECTION_CHARS = {"zh": 10, "en": 40}

# 送审时必须由法务/运营拍板、代码代不了的事项（始终打印，作为送审清单）
PENDING_ITEMS = (
    "运营主体名称与联系方式：终稿「六、联系方式」需落到具体主体与邮箱，不能只写「平台运营方公布的联系方式」",
    "适用法律与争议解决条款：是否约定管辖/仲裁，待法务给出",
    "个人信息保存期限与到期处理：待法务给出年限口径",
    "英文版效力：现按「中文为准、英文为参考译文」声明；若需中英同等效力，须法务确认后同步改 disclaimer 与 meta.authoritative_lang",
    "简历原件加密存储（KMS/应用层 AES）：隐私政策第四节已如实披露「尚未实现」，上线前需完成后再删该句",
)


def _load_text() -> dict:
    return json.loads(TEXT_FILE.read_text(encoding="utf-8"))


def _blocks(section: dict) -> list[dict]:
    return section.get("body") or []


def _plain(value: str) -> str:
    return str(value).replace("**", "")


def _section_text(section: dict) -> str:
    """小节全文（去掉粗体标记），用于字数下限与占位词/中文残留扫描。"""
    parts: list[str] = []
    for block in _blocks(section):
        parts.extend(block["ul"] if "ul" in block else [block.get("p", "")])
    return _plain("".join(parts))


def validate(text: dict) -> list[str]:
    """结构契约：返回问题列表（非空即 exit 2）。"""
    problems: list[str] = []
    ids_by_doc: dict[str, set[str]] = {}

    for doc_key, (zh_label, _) in DOC_LABELS.items():
        langs = text.get(doc_key) or {}
        zh, en = langs.get("zh") or [], langs.get("en") or []
        if not zh or not en:
            problems.append(f"{zh_label}：中英正文不完整（zh {len(zh)} 节 / en {len(en)} 节）")
            continue
        ids_by_doc[doc_key] = {s["id"] for s in zh}
        if [s["id"] for s in zh] != [s["id"] for s in en]:
            problems.append(
                f"{zh_label}：中英小节 id/顺序不一致——"
                f"zh {[s['id'] for s in zh]} vs en {[s['id'] for s in en]}"
            )
            continue

        for zh_sec, en_sec in zip(zh, en):
            sid = zh_sec["id"]
            zh_blocks, en_blocks = _blocks(zh_sec), _blocks(en_sec)
            # 段落结构对齐：块数、类型（p/ul）、列表条数都要一一对应，否则会出现"英文漏了一段"
            if len(zh_blocks) != len(en_blocks):
                problems.append(f"{zh_label}/{sid}：中英段落数不一致（{len(zh_blocks)} vs {len(en_blocks)}）")
                continue
            for i, (bz, be) in enumerate(zip(zh_blocks, en_blocks)):
                if ("ul" in bz) != ("ul" in be):
                    problems.append(f"{zh_label}/{sid} 第 {i + 1} 段：中英块类型不一致（p/ul）")
                elif "ul" in bz and len(bz["ul"]) != len(be["ul"]):
                    problems.append(
                        f"{zh_label}/{sid} 第 {i + 1} 段：中英列表条数不一致"
                        f"（{len(bz['ul'])} vs {len(be['ul'])}）"
                    )
            for lang, sec in (("zh", zh_sec), ("en", en_sec)):
                body = _section_text(sec)
                if len(body) < MIN_SECTION_CHARS[lang]:
                    problems.append(f"{zh_label}/{sid}[{lang}]：小节内容过短（{len(body)} 字），疑似未写")
                for marker in PLACEHOLDERS:
                    if marker in body:
                        problems.append(f"{zh_label}/{sid}[{lang}]：残留占位词「{marker}」")
            # 英文版不允许残留中文（漏译最常见的形态就是把中文黏在英文段落里）
            cjk_hit = CJK.search(_section_text(en_sec) + en_sec["h"])
            if cjk_hit:
                problems.append(f"{zh_label}/{sid}[en]：英文小节残留中文「{cjk_hit.group(0)}」")

    # 强制披露三节（§14 ①）：中英两套都得有
    for rid in REQUIRED_SECTION_IDS:
        holders = [k for k, ids in ids_by_doc.items() if rid in ids]
        if not holders:
            problems.append(f"强制披露小节缺失：{rid}（见后端 legal.REQUIRED_SECTION_IDS）")
        for doc_key in holders:
            langs = text.get(doc_key) or {}
            if not any(s["id"] == rid for s in langs.get("en") or []):
                problems.append(f"强制披露小节 {rid} 在 {DOC_LABELS[doc_key][0]} 的英文版中缺失")

    # 效力声明与 meta.authoritative_lang 口径必须一致（声明说"中文为准"就不能反过来）
    meta = text.get("meta") or {}
    authoritative = meta.get("authoritative_lang")
    disclaimer = text.get("disclaimer") or {}
    if authoritative not in LANGS:
        problems.append(f"meta.authoritative_lang 必须是 {'/'.join(LANGS)}，当前为 {authoritative!r}")
    elif not disclaimer.get(authoritative):
        problems.append(f"disclaimer 缺 {authoritative} 版本（效力声明必须与 authoritative_lang 同语言齐备）")
    elif "解释依据" not in disclaimer["zh"] and "为准" not in disclaimer["zh"]:
        problems.append("disclaimer[zh] 未写明中文版本为准（与 meta.authoritative_lang 口径不符）")
    elif "prevail" not in disclaimer["en"]:
        problems.append("disclaimer[en] 未写明英文为参考译文（与 meta.authoritative_lang 口径不符）")

    # 前端硬编码版本 vs 后端单一事实源（与 tests/test_legal.py 同口径，这里给送审出口再兜一层）
    shell = SHELL_FILE.read_text(encoding="utf-8")
    hit = re.search(r'POLICY_VERSION = "([^"]+)"', shell)
    if not hit or hit.group(1) != POLICY_VERSION:
        problems.append(
            f"前端 legal-doc.js 版本（{hit.group(1) if hit else '未找到'}）与后端 POLICY_VERSION（{POLICY_VERSION}）不一致"
        )
    return problems


def review_gaps(text: dict) -> list[str]:
    """法务签署状态（非代码事项）：未闭环即 exit 1。"""
    review = (text.get("meta") or {}).get("legal_review") or {}
    gaps: list[str] = []
    if not review.get("reviewer"):
        gaps.append("法务复核人未填写（meta.legal_review.reviewer）")
    if not review.get("reviewed_at"):
        gaps.append("法务复核日期未填写（meta.legal_review.reviewed_at）")
    signed = review.get("signed_version") or ""
    if signed != POLICY_VERSION:
        detail = "未填写" if not signed else f"签署于 v{signed}"
        gaps.append(f"签署版本与当前政策不一致（{detail} / 当前 v{POLICY_VERSION}）——政策改版会让原签署失效，须重新复核")
    return gaps


def render(text: dict, langs: tuple[str, ...]) -> str:
    lines: list[str] = [
        f"法务送审稿 ｜ 政策版本 v{POLICY_VERSION} ｜ 生效日 {POLICY_EFFECTIVE_DATE}",
        f"解释依据：{text.get('meta', {}).get('authoritative_lang')} 版本（另一语言为参考译文）",
        "",
    ]
    for doc_key, (zh_label, en_label) in DOC_LABELS.items():
        langs_map = text.get(doc_key) or {}
        zh_sections = langs_map.get("zh") or []
        en_sections = langs_map.get("en") or []
        lines.append(f"=== {zh_label} / {en_label}（{len(zh_sections)} 节） ===")
        for i, zh_sec in enumerate(zh_sections):
            en_sec = en_sections[i] if i < len(en_sections) else {"h": "", "body": []}
            lines.append("")
            lines.append(f"【{zh_sec['h']} / {en_sec.get('h', '')}】")
            for lang, sec in (("zh", zh_sec), ("en", en_sec)):
                if lang not in langs:
                    continue
                for block in _blocks(sec):
                    if "ul" in block:
                        for item in block["ul"]:
                            lines.append(f"  [{lang}] - {_plain(item)}")
                    else:
                        lines.append(f"  [{lang}] {_plain(block.get('p', ''))}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", choices=("zh", "en", "both"), default="both", help="输出语种（默认中英对照）")
    parser.add_argument("--out", help="写入文件（默认打印到 stdout）")
    args = parser.parse_args()

    text = _load_text()

    problems = validate(text)
    if problems:
        for p in problems:
            print(f"[结构错误] {p}", file=sys.stderr)
        return 2

    langs = ("zh", "en") if args.lang == "both" else (args.lang,)
    body = render(text, langs)
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(body)

    required = "、".join(REQUIRED_SECTION_IDS)
    print(f"结构校验：通过（中英小节与段落一一对应；强制披露 {required} 中英齐备）")
    print(f"效力声明：{text['disclaimer'][text['meta']['authoritative_lang']][:40]}…")

    gaps = review_gaps(text)
    print()
    print("送审待确认（非代码，法务/运营拍板）：")
    for item in PENDING_ITEMS:
        print(f"- {item}")
    if gaps:
        print()
        print("法务签署：未闭环")
        for g in gaps:
            print(f"- {g}")
        return 1
    review = text["meta"]["legal_review"]
    print()
    print(f"法务签署：已闭环（复核人 {review['reviewer']}，复核日 {review['reviewed_at']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())