# -*- coding: utf-8 -*-
"""大疆第七轮:分析 Moka jobs/v2 调用方式并实测;顺带测网易 pageSize=50。"""
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
           "Referer": "https://apply.careers.dji.com/social-recruitment/dji/170070?hash=%23%2Fjobs",
           "Origin": "https://apply.careers.dji.com"}
BASE = "https://apply.careers.dji.com"


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as c:
        # 1) 找 bundle 中 jobs/v2 的调用上下文
        page = c.get(BASE + "/social-recruitment/dji/170070").text
        srcs = []
        for s in re.findall(r'<script[^>]+src="([^"]+)"', page):
            if s.startswith("//"):
                srcs.append("https:" + s)
            elif s.startswith("http"):
                srcs.append(s)
            elif s.startswith("/"):
                srcs.append(BASE + s)
        ctx_shown = 0
        for u in srcs:
            if not u.endswith(".js"):
                continue
            try:
                rb = c.get(u, timeout=30)
                if rb.status_code != 200:
                    continue
            except Exception:
                continue
            js = rb.text
            for kw in ["jobs/v2"]:
                for m in re.finditer(re.escape(kw), js):
                    s0 = max(0, m.start() - 350)
                    frag = js[s0:m.end() + 350].replace("\n", " ")
                    print("=== %s | ...%s... ===" % (u[-40:], frag[:700]))
                    ctx_shown += 1
        if not ctx_shown:
            print("no jobs/v2 context found in bundles")
        # 2) 实测 jobs/v2
        ep = BASE + "/api/outer/ats-apply/website/jobs/v2"
        for body in [{"currentPage": 1, "pageSize": 10},
                     {"projectId": 170070, "currentPage": 1, "pageSize": 10, "keyword": ""},
                     {"siteId": 170070, "currentPage": 1, "pageSize": 10}]:
            try:
                rr = c.post(ep, json=body, timeout=25)
                print("POST %s %s -> %s" % (ep, body, rr.status_code))
                print("  body[:400]:", rr.text[:400].replace("\n", " "))
            except Exception as e:
                print("POST %s -> ERROR %s" % (ep, e))
        # 3) 网易 pageSize=50
        nep = "https://hr.163.com/api/hr163/position/queryPage"
        nr = c.post(nep, json={"currentPage": 1, "pageSize": 50, "keyword": "",
                               "workPlaceList": [], "jobTypeId": ""},
                    headers={"Content-Type": "application/json",
                             "Referer": "https://hr.163.com/",
                             "Origin": "https://hr.163.com"}, timeout=25)
        d = nr.json()["data"]
        print("网易 pageSize=50 -> total=%s pages=%s items=%s" % (
            d.get("total"), d.get("pages"), len(d.get("list") or [])))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
