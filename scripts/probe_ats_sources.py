"""M1 数据源可用性探测脚本

用途：在开发 ATS 适配器之前，先验证候选站点的公开接口是否真实可用。
不依赖任何三方库（仅 stdlib），可直接运行：
    python scripts/probe_ats_sources.py

输出：控制台表格 + scripts/probe_result.json
"""
import json
import ssl
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; JobDirectApply/0.1; +https://example.com/bot)"
TIMEOUT = 20


def _req(url, method="GET", body=None, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read()
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def _count(payload, keys):
    """从 JSON 响应里数出职位条数（按候选路径逐个尝试）。"""
    for path in keys:
        cur = payload
        ok = True
        for seg in path.split("."):
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                ok = False
                break
        if ok and isinstance(cur, list):
            return len(cur)
    return None


def probe_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=false"
    code, raw = _req(url)
    n = None
    if code == 200:
        try:
            n = _count(json.loads(raw), ["jobs"])
        except Exception:  # noqa: BLE001
            pass
    return {"source": "greenhouse", "slug": slug, "url": url, "http": code, "jobs": n}


def probe_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    code, raw = _req(url)
    n = None
    if code == 200:
        try:
            j = json.loads(raw)
            n = len(j) if isinstance(j, list) else None
        except Exception:  # noqa: BLE001
            pass
    return {"source": "lever", "slug": slug, "url": url, "http": code, "jobs": n}


def probe_workday(tenant, host, site):
    url = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    code, raw = _req(url, method="POST", body={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    n = None
    if code == 200:
        try:
            j = json.loads(raw)
            n = j.get("total")
        except Exception:  # noqa: BLE001
            pass
    return {"source": "workday", "slug": f"{tenant}/{site}", "url": url, "http": code, "jobs": n}


def probe_official(name, url, method="GET", body=None, keys=("Data.Posts", "data.list", "data", "content.positionList")):
    code, raw = _req(url, method=method, body=body)
    n = None
    if code == 200 and raw:
        try:
            n = _count(json.loads(raw), list(keys))
        except Exception:  # noqa: BLE001
            pass
    return {"source": "official_site", "slug": name, "url": url, "http": code, "jobs": n}


GREENHOUSE = ["stripe", "coinbase", "databricks", "figma", "robinhood", "dropbox",
              "gitlab", "airbnb", "discord", "reddit", "twilio", "hubspot", "cloudflare", "samsara"]

LEVER = ["kraken", "plaid", "benchling", "attentive", "netflix", "lyft", "hopper", "mistral", "ramp"]

WORKDAY = [
    ("nvidia", "nvidia.wd5", "NVIDIAExternalCareerSite"),
    ("intel", "intel.wd1", "External"),
    ("cisco", "cisco.wd5", "Cisco_Careers"),
    ("dell", "dell.wd1", "External"),
    ("adobe", "adobe.wd5", "external_experienced"),
    ("salesforce", "salesforce.wd12", "External_Career_Site"),
    ("hp", "hp.wd5", "ExternalCareerSite"),
    ("zoom", "zoom.wd5", "Zoom"),
]

OFFICIAL = [
    ("腾讯招聘", "https://careers.tencent.com/tencentcareer/api/post/Query?pageIndex=1&pageSize=10&language=zh-cn"),
    ("百度招聘", "https://talent.baidu.com/httservice/getPostListNew", "POST", {"recruitType": "SOCIAL", "pageSize": 10, "curPage": 1}),
    ("网易招聘", "https://hr.163.com/api/hr163/position/queryPage", "POST", {"currentPage": 1, "pageSize": 10}),
    ("美团招聘", "https://zhaopin.meituan.com/api/official/job/jobListV2", "POST", {"pageNo": 1, "pageSize": 10}),
]


def main():
    results = []
    for slug in GREENHOUSE:
        results.append(probe_greenhouse(slug))
        time.sleep(0.3)
    for slug in LEVER:
        results.append(probe_lever(slug))
        time.sleep(0.3)
    for tenant, host, site in WORKDAY:
        results.append(probe_workday(tenant, host, site))
        time.sleep(0.3)
    for name, url, *rest in OFFICIAL:
        method = rest[0] if len(rest) > 0 else "GET"
        body = rest[1] if len(rest) > 1 else None
        results.append(probe_official(name, url, method=method, body=body))
        time.sleep(0.3)

    print(f"{'source':<14}{'slug':<34}{'http':<6}{'jobs':<8}")
    print("-" * 64)
    for r in results:
        print(f"{r['source']:<14}{r['slug']:<34}{str(r['http']):<6}{str(r['jobs'] if r['jobs'] is not None else '-'):<8}")

    ok = [r for r in results if r["http"] == 200]
    print(f"\n可用 {len(ok)} / 共 {len(results)}")

    with open("scripts/probe_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
