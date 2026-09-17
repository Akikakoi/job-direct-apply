"""第二批探测：Lever 真实 slug 搜捕 + 国内站点结构/robots 核实。"""
import json
import ssl
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; JobDirectApply/0.1; +https://example.com/bot)"


def get(url, method="GET", body=None, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*, text/html"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


LEVER_GUESS = ["weee", "gopuff", "scribd", "carta", "lattice", "render", "sourcegraph", "pagerduty",
               "eventbrite", "betterment", "tinder", "seatgeek", "flexport", "affirm", "marqeta",
               "chime", "gusto", "amid", "kavak", "nu", "loft", "carvana", "drweng", "sardine",
               "shield", "together", "unify", "vanta", "whatnot", "zeplin", "axoni", "pipe",
               "jobber", "fetch", "goat", "hive", "instabase", "jumpcloud", "klaviyo", "mixpanel"]

print("== Lever slug 搜捕 ==")
lever_ok = []
for slug in LEVER_GUESS:
    code, raw = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    n = None
    if code == 200:
        try:
            j = json.loads(raw)
            n = len(j) if isinstance(j, list) else None
        except Exception:  # noqa: BLE001
            n = None
    if code == 200 and n:
        lever_ok.append((slug, n))
        print(f"  HIT  {slug:<16} jobs={n}")
    time.sleep(0.2)
print(f"  命中 {len(lever_ok)} 个：{lever_ok}")

print("\n== 国内站点响应结构 ==")
for name, url, method, body in [
    ("腾讯", "https://careers.tencent.com/tencentcareer/api/post/Query?pageIndex=1&pageSize=3&language=zh-cn", "GET", None),
    ("百度", "https://talent.baidu.com/httservice/getPostListNew", "POST", {"recruitType": "SOCIAL", "pageSize": 3, "curPage": 1}),
    ("网易", "https://hr.163.com/api/hr163/position/queryPage", "POST", {"currentPage": 1, "pageSize": 3}),
    ("字节", "https://jobs.bytedance.com/api/v1/search/job/posts?page=1&limit=3", "POST", {}),
    ("小米", "https://hr.xiaomi.com/api/pc/social/position/list?page=1&pageSize=3", "GET", None),
]:
    code, raw = get(url, method=method, body=body)
    head = raw[:400].decode("utf-8", "ignore").replace("\n", " ")
    print(f"\n-- {name} http={code} len={len(raw)}")
    print(f"   {head}")

print("\n== robots.txt ==")
for name, host in [("腾讯", "careers.tencent.com"), ("百度", "talent.baidu.com"),
                   ("网易", "hr.163.com"), ("字节", "jobs.bytedance.com"),
                   ("小米", "hr.xiaomi.com")]:
    code, raw = get(f"https://{host}/robots.txt")
    text = raw.decode("utf-8", "ignore").strip()
    print(f"\n-- {name} ({host}) http={code}")
    print("   " + (text[:300].replace("\n", "\n   ") if text else "(空)"))

print("\n== Lever/Greenhouse 详情字段抽样 ==")
code, raw = get("https://api.lever.co/v0/postings/kraken?mode=json")
print("lever kraken raw head:", raw[:200].decode("utf-8", "ignore"))
code, raw = get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true")
if code == 200:
    j = json.loads(raw)
    job = j["jobs"][0]
    print("greenhouse stripe job keys:", sorted(job.keys()))
    print("location sample:", job.get("location"))
