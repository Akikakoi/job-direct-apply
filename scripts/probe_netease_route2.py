# -*- coding: utf-8 -*-
"""看 job-detail 路由的参数形式。"""
import re
import sys

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def main():
    c = httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
    js = c.get("https://hr.163.com/static/js/commons.c65656b8.chunk.js").text
    for m in list(re.finditer(r"job-detail", js))[:6]:
        s0 = max(0, m.start() - 90)
        print("...", js[s0:m.end() + 90].replace("\n", " "), "...")
        print("-" * 60)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
