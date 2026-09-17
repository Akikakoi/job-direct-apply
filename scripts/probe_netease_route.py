# -*- coding: utf-8 -*-
"""确认网易 SPA 的职位详情路由格式。"""
import re
import sys

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def main():
    c = httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
    for u in ["https://hr.163.com/static/js/commons.c65656b8.chunk.js",
              "https://hr.163.com/static/js/index.7b551ceb.js"]:
        js = c.get(u).text
        pats = sorted(set(re.findall(
            r'["\']((?:/position|/job|/posts)[a-zA-Z0-9/_\-]{0,40})["\']', js)))
        print(u.rsplit("/", 1)[-1], "->", pats[:15])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
