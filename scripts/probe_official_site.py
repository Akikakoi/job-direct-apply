"""官网兜底适配器（official_site）逐站评估探测：robots 是否允许、页面有没有可解析的职位数据。

用于 §5.3 的"逐站评估"环节：先跑本脚本拿到证据，再决定
① 能直接用（页面挂 JSON-LD JobPosting）② 需要补 html_rules（给出骨架）
③ 不该采（robots 不允许 / 数据加密，标记休眠）。

只读、只请求 robots.txt 与目标页各一次、不写入任何库。

用法（backend/ 或仓库根均可）：
    ../.venv/Scripts/python.exe ../scripts/probe_official_site.py https://example.com/careers
    ../.venv/Scripts/python.exe ../scripts/probe_official_site.py --json https://example.com/careers
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.adapters.official_site import (  # noqa: E402
    html_to_text,
    json_ld_blocks,
    find_job_postings,
    parse_robots,
    scripts_in,
)
from app.core.config import settings  # noqa: E402


def probe(url: str, as_json: bool = False) -> int:
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    headers = {"User-Agent": settings.ua}
    report: dict = {"url": url, "robots_allowed": None, "robots_status": None, "page_status": None}

    try:
        robots = httpx.get(f"{base}/robots.txt", headers=headers, timeout=20)
        report["robots_status"] = robots.status_code
        report["robots_allowed"] = robots.status_code >= 400 or parse_robots(robots.text, url)
        report["robots_allowed_note"] = (
            "robots 404/5xx → 视为允许（仍按 official_site_interval_min 低频）"
            if robots.status_code >= 400
            else None
        )
    except Exception as exc:
        report["robots_allowed"] = None
        report["robots_error"] = f"{type(exc).__name__}: {exc}"

    jobs: list[dict] = []
    if report["robots_allowed"] is not False:
        try:
            page = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
            report["page_status"] = page.status_code
            report["page_bytes"] = len(page.content)
            html = page.text or ""
            blocks = json_ld_blocks(html)
            report["ld_json_blocks"] = len(blocks)
            jobs = find_job_postings(blocks)
            report["job_postings"] = len(jobs)
            report["script_ids"] = sorted({s["id"] for s in scripts_in(html) if s["id"]})[:10]
            report["json_scripts"] = sorted(
                {s["type"] for s in scripts_in(html) if "json" in (s["type"] or "").lower()}
            )[:6]
        except Exception as exc:
            report["page_error"] = f"{type(exc).__name__}: {exc}"

    if as_json:
        sample = None
        if jobs:
            j = jobs[0]
            sample = {
                "title": j.get("title"),
                "url": j.get("url"),
                "datePosted": j.get("datePosted"),
                "jobLocation": j.get("jobLocation"),
                "has_description": bool(j.get("description")),
            }
        print(json.dumps({**report, "sample": sample}, ensure_ascii=False, indent=2))
        return 0

    print(f"URL: {url}")
    print(f"  robots.txt: HTTP {report['robots_status']} → 允许抓取 = {report['robots_allowed']}")
    if report.get("page_status"):
        print(f"  页面: HTTP {report['page_status']}｜{report['page_bytes']} 字节")
        print(f"  JSON-LD 块: {report['ld_json_blocks']}｜其中 JobPosting: {report['job_postings']}")
        if report.get("script_ids"):
            print(f"  带 id 的 script: {report['script_ids']}")
        if jobs:
            j = jobs[0]
            print("  首个职位（规范化后字段）：")
            print(f"    title       = {j.get('title')!r}")
            print(f"    url         = {j.get('url')!r}")
            print(f"    datePosted  = {j.get('datePosted')!r}")
            print(f"    city        = {json.dumps(j.get('jobLocation'), ensure_ascii=False)[:90]}")
            desc = html_to_text(str(j.get('description') or ''))[:80]
            print(f"    description = {desc!r}")
            print("  评估结论：可直接接入（fetch_policy.html_rules = {list_url: <本页>}）")
        else:
            print("  评估结论：页面无 JSON-LD JobPosting → 需要补 html_rules（json/regex 模式），")
            print("            或按 §5.3 判定为休眠（数据加密 / 需登录 / 条款不允许）。")
            print("  规则骨架（填好选择器/正则后写进 company.fetch_policy）：")
            skeleton = {
                "html_rules": {
                    "list_url": url,
                    "max_pages": 3,
                    "json": {"script_id": "<如 __NEXT_DATA__>", "json_path": "<如 props.pageProps.jobs>"},
                    "fields": {"title": "<字段路径或正则>", "url": "<字段路径或正则>"},
                    "external_id_from": "url",
                }
            }
            print(json.dumps(skeleton, ensure_ascii=False, indent=2))
    else:
        print(f"  未抓取页面（robots 不允许或请求失败）：{report.get('page_error') or 'robots disallow'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="待评估的官网职位页 URL")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于留痕/对比）")
    args = parser.parse_args()
    return probe(args.url, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
