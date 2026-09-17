# -*- coding: utf-8 -*-
"""大疆第六轮:apply.careers.dji.com(Moka 系 ATS)页面与 API 探测。"""
import json
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Referer": "https://apply.careers.dji.com/"}


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True) as c:
        url = "https://apply.careers.dji.com/social-recruitment/dji/170070"
        r = c.get(url, timeout=30)
        html = r.text
        print("GET %s -> %s len=%s" % (url, r.status_code, len(html)))
        # Moka 是 Nuxt:找 __NUXT__
        for marker in ["__NUXT__", "__INITIAL_STATE__", "window.__", "api"]:
            idx = html.find(marker)
            print("marker %s: %s" % (marker, "FOUND@" + str(idx) if idx >= 0 else "no"))
        # script srcs
        srcs = []
        for s in re.findall(r'<script[^>]+src="([^"]+)"', html):
            if s.startswith("//"):
                srcs.append("https:" + s)
            elif s.startswith("http"):
                srcs.append(s)
            elif s.startswith("/"):
                srcs.append("https://apply.careers.dji.com" + s)
        print("scripts: %d" % len(srcs))
        for s in srcs[:12]:
            print("  ", s)
        # bundle 找 api 路径
        api_re = re.compile(r'["\'](/api/[^"\']{2,90})["\']')
        seen = {}
        for u in srcs:
            if not u.endswith(".js"):
                continue
            try:
                rb = c.get(u, timeout=30)
                if rb.status_code != 200:
                    continue
            except Exception:
                continue
            for hit in api_re.findall(rb.text):
                seen.setdefault(hit, u[-40:])
        print("--- api paths in bundles ---")
        for p, src in sorted(seen.items()):
            print("  %s   (from %s)" % (p, src))
        # Moka 已知 outer 接口猜测
        guesses = [
            ("POST", "https://apply.careers.dji.com/api/outer/pc/position/list",
             {"projectId": 170070, "currentPage": 1, "pageSize": 10}),
        ]
        for method, ep, body in guesses:
            try:
                rr = c.request(method, ep, json=body, timeout=25)
                print("%s %s -> %s ct=%s" % (method, ep, rr.status_code,
                                             rr.headers.get("content-type")))
                print("  body[:400]:", rr.text[:400].replace("\n", " "))
            except Exception as e:
                print("%s %s -> ERROR %s" % (method, ep, e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
