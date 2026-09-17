# -*- coding: utf-8 -*-
"""测网易详情接口 /api/hr163/position/query 与 job-detail URL 形式。"""
import json
import sys

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
H = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
     "Content-Type": "application/json", "Referer": "https://hr.163.com/",
     "Origin": "https://hr.163.com"}


def main():
    c = httpx.Client(headers=H, timeout=25, follow_redirects=True)
    pid = 58384
    # GET / POST 各种形式
    tries = [
        ("GET", "https://hr.163.com/api/hr163/position/query?id=%d" % pid, None),
        ("POST", "https://hr.163.com/api/hr163/position/query", {"id": pid}),
        ("POST", "https://hr.163.com/api/hr163/position/query", {"postId": pid}),
    ]
    for method, u, body in tries:
        r = c.request(method, u, json=body)
        head = r.text[:260].replace("\n", " ")
        print("%s %s %s -> %s | %s" % (method, u, body or "", r.status_code, head))
    # job-detail 页面对 id 的反应(看 title 或路由数据)
    r = c.get("https://hr.163.com/job-detail/%d" % pid,
              headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    print("GET /job-detail/%d -> %s len=%s title_hit=%s" % (
        pid, r.status_code, len(r.text), "job-detail" in r.text))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
