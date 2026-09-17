# -*- coding: utf-8 -*-
"""大疆 __NEXT_DATA__ 解包(修正版):page 是字符串路由,数据在 props.pageProps。"""
import json
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}


def walk(obj, path, depth=0):
    hits = []
    if depth > 7:
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(w in kl for w in ("job", "position", "recruit", "list")):
                if isinstance(v, dict):
                    sample = "dict(%d, keys=%s)" % (len(v), sorted(v.keys())[:10])
                elif isinstance(v, list):
                    sample = "list(%d)" % len(v)
                else:
                    sample = str(v)[:100]
                hits.append((path + "/" + str(k), sample))
            hits.extend(walk(v, path + "/" + str(k), depth + 1))
    elif isinstance(obj, list) and obj:
        hits.extend(walk(obj[0], path + "[0]", depth + 1))
    return hits


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True) as c:
        r = c.get("https://we.dji.com/zh-CN/list", timeout=25)
        html = r.text
        print("GET /zh-CN/list -> %s len=%s" % (r.status_code, len(html)))
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
                      html, re.S)
        if not m:
            print("no __NEXT_DATA__")
            return
        nd = json.loads(m.group(1))
        print("buildId =", nd.get("buildId"))
        print("page route =", nd.get("page"))
        props = nd.get("props", {})
        pprop = props.get("pageProps", {})
        print("props keys:", sorted(props.keys()))
        print("pageProps keys:", sorted(pprop.keys()) if isinstance(pprop, dict) else type(pprop))
        print()
        print("--- job/position/list 相关路径 ---")
        for p, s in walk(nd, "ND")[:30]:
            print("  %s: %s" % (p, s))
        # Next.js 数据路由探测
        build = nd.get("buildId")
        route = str(nd.get("page") or "/list")
        if build:
            dr = "https://we.dji.com/_next/data/%s/zh-CN%s.json" % (build, route)
            try:
                rd = c.get(dr, timeout=25)
                print("GET %s -> %s ct=%s len=%s" % (dr, rd.status_code,
                                                     rd.headers.get("content-type"),
                                                     len(rd.text)))
                if rd.status_code == 200 and "json" in (rd.headers.get("content-type") or ""):
                    body = rd.json()
                    pp = body.get("pageProps", {})
                    print("  data-route pageProps keys:",
                          sorted(pp.keys()) if isinstance(pp, dict) else type(pp))
            except Exception as e:
                print("GET %s -> ERROR %s" % (dr, e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
