"""探测 Ashby / SmartRecruiters 公开招聘 API（P4 新 ATS 评估，证据留痕）。

Ashby:  GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true
SmartRecruiters: GET https://api.smartrecruiters.com/v1/companies/{company}/postings

用法：backend/ 下 ../.venv/Scripts/python.exe ../scripts/probe_ashby_sr.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.config import settings  # noqa: E402

UA = settings.ua

ASHBY_ORGS = ["ashby", "elevenlabs", "watershed", "linear", "vercel"]
SR_COMPANIES = ["Visa", "Bose", "Equinox", "Tanium", "Ocado"]


def probe(name: str, url: str) -> None:
    try:
        resp = httpx.get(url, headers={"User-Agent": UA}, timeout=20)
    except Exception as exc:
        print(f"[{name}] EXC {type(exc).__name__}: {exc}")
        return
    ct = resp.headers.get("content-type", "")
    print(f"[{name}] HTTP {resp.status_code} {ct}")
    if resp.status_code != 200 or "json" not in ct:
        print(f"   body[:120]={resp.text[:120]!r}")
        return
    data = resp.json()
    jobs = data.get("jobs") or data.get("content") or []
    print(f"   keys={list(data)[:8]} jobs={len(jobs)}")
    if jobs:
        j = jobs[0]
        print(f"   sample keys={list(j)[:14]}")
        print(f"   sample={ {k: str(v)[:60] for k, v in list(j.items())[:8]} }")


def main() -> int:
    print("== Ashby ==")
    for org in ASHBY_ORGS:
        probe(f"ashby:{org}", f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
    print("== SmartRecruiters ==")
    for c in SR_COMPANIES:
        probe(f"sr:{c}", f"https://api.smartrecruiters.com/v1/companies/{c}/postings?limit=10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
