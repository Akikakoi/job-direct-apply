# -*- coding: utf-8 -*-
"""探查网易招聘 / 大疆招聘的页面结构与公开 JSON 接口。

目的:为 OfficialSiteAdapter 定型提供依据 —— 页面是否服务端渲染、
是否有公开 JSON 接口、接口的鉴权/加密程度如何。只读探测,不抓取详情。
"""
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}

API_RE = re.compile(r'["\'](/(?:api|gateway|hr163|recruit)[^"\']{2,90})["\']')
FULL_API_RE = re.compile(r'["\'](https?://[^"\']*api[^"\']{2,90})["\']')
SKIP = ("w3.org", "npmjs.com", "github", "googleapis", "schema.org",
        "webpack", "adobe", "sentry", "umeng")


def fetch(client, url, **kw):
    try:
        r = client.get(url, timeout=25, **kw)
        return r.status_code, r.text
    except Exception as e:
        return None, "ERROR %s" % e


def script_srcs(html, base):
    out = []
    for s in re.findall(r'<script[^>]+src="([^"]+)"', html):
        if s.startswith("//"):
            out.append("https:" + s)
        elif s.startswith("http"):
            out.append(s)
        elif s.startswith("/"):
            out.append(base + s)
    return out


def api_hits(text):
    hits = set(API_RE.findall(text)) | set(FULL_API_RE.findall(text))
    return sorted(h for h in hits if not any(s in h for s in SKIP))


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True) as c:
        # ---------- 网易 ----------
        print("=" * 20, "网易 hr.163.com", "=" * 20)
        code, html = fetch(c, "https://hr.163.com/")
        print("GET / -> %s len=%s" % (code, len(html)))
        netease_srcs = []
        if code == 200:
            netease_srcs = script_srcs(html, "https://hr.163.com")
            print("script tags:", len(netease_srcs))
            for s in netease_srcs[:10]:
                print("  ", s)
            print("api hints in html:", api_hits(html)[:10])
            # 已知历史 POST 接口
            ep = "https://hr.163.com/api/hr163/position/queryPage"
            body = {"currentPage": 1, "pageSize": 10, "keyword": "",
                    "workPlaceList": [], "jobTypeId": ""}
            try:
                r = c.post(ep, json=body, headers={
                    "Content-Type": "application/json",
                    "Referer": "https://hr.163.com/"}, timeout=25)
                print("POST %s -> %s ct=%s" % (ep, r.status_code,
                                               r.headers.get("content-type")))
                print("  body[:500]:", r.text[:500].replace("\n", " "))
            except Exception as e:
                print("POST %s -> ERROR %s" % (ep, e))
            # JS bundle 找接口
            for s in netease_srcs:
                if not s.endswith(".js"):
                    continue
                c2, js = fetch(c, s)
                if c2 == 200 and js:
                    hits = api_hits(js)
                    if hits:
                        print("bundle ...%s api hits: %s" % (s[-55:], hits[:18]))
        # ---------- 大疆 ----------
        print()
        print("=" * 20, "大疆 we.dji.com", "=" * 20)
        code, html = fetch(c, "https://we.dji.com/zh-CN")
        print("GET /zh-CN -> %s len=%s" % (code, len(html)))
        dji_srcs = []
        if code == 200:
            dji_srcs = script_srcs(html, "https://we.dji.com")
            print("script tags:", len(dji_srcs))
            for s in dji_srcs[:10]:
                print("  ", s)
            print("api hints in html:", api_hits(html)[:10])
            for marker in ["__INITIAL_STATE__", "__NEXT_DATA__",
                           "window.__", "__NUXT__", "JSON.parse"]:
                idx = html.find(marker)
                print("marker %s: %s" % (marker,
                                         "FOUND@" + str(idx) if idx >= 0 else "no"))
            for s in dji_srcs:
                if not s.endswith(".js"):
                    continue
                c2, js = fetch(c, s)
                if c2 == 200 and js:
                    hits = api_hits(js)
                    if hits:
                        print("bundle ...%s api hits: %s" % (s[-55:], hits[:20]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
