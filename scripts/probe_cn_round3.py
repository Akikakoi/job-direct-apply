# -*- coding: utf-8 -*-
"""第三轮探测:网易接口响应结构细化 + 大疆 __NEXT_DATA__/数据路由解包。"""
import json
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True) as c:
        # ---------- 网易 ----------
        print("=" * 20, "网易 结构细化", "=" * 20)
        ep = "https://hr.163.com/api/hr163/position/queryPage"
        body = {"currentPage": 1, "pageSize": 10, "keyword": "",
                "workPlaceList": [], "jobTypeId": ""}
        r = c.post(ep, json=body, headers={
            "Content-Type": "application/json",
            "Referer": "https://hr.163.com/"}, timeout=25)
        data = r.json()["data"]
        lst = data.get("list", [])
        print("total=%s pages=%s page_items=%s" % (
            data.get("total"), data.get("pages"), len(lst)))
        if lst:
            item = lst[0]
            print("item keys:", sorted(item.keys()))
            for k in ["id", "name", "workPlace", "firstPostTypeName",
                      "recruitNum", "publishTime", "updateTime", "storageId",
                      "city", "jobTypeName"]:
                if k in item:
                    print("  %s = %s" % (k, str(item[k])[:80]))
        # 翻页 + 关键词过滤是否有效
        body2 = {"currentPage": 2, "pageSize": 10, "keyword": "Python",
                 "workPlaceList": [], "jobTypeId": ""}
        r2 = c.post(ep, json=body2, headers={
            "Content-Type": "application/json",
            "Referer": "https://hr.163.com/"}, timeout=25)
        d2 = r2.json()["data"]
        print("keyword=Python -> total=%s first=%s" % (
            d2.get("total"),
            (d2.get("list") or [{}])[0].get("name", "")))
        # 详情接口猜测:posts/{id}
        if lst:
            pid = lst[0].get("id")
            for guess in ["https://hr.163.com/api/hr163/position/post/detail?id=%s" % pid,
                          "https://hr.163.com/api/hr163/post/detail?id=%s" % pid]:
                rg = c.get(guess, timeout=20)
                print("GET %s -> %s len=%s" % (guess, rg.status_code,
                                               len(rg.text)))
        # ---------- 大疆 ----------
        print()
        print("=" * 20, "大疆 __NEXT_DATA__ 解包", "=" * 20)
        r = c.get("https://we.dji.com/zh-CN/list", timeout=25)
        html = r.text
        print("GET /zh-CN/list -> %s len=%s" % (r.status_code, len(html)))
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
                      html, re.S)
        if not m:
            print("no __NEXT_DATA__ on /list")
            return
        nd = json.loads(m.group(1))
        print("buildId =", nd.get("buildId"))
        page = nd.get("page", {})
        print("page keys:", sorted(page.keys()))
        props = page.get("props", {})
        print("props keys:", sorted(props.keys()))
        pprop = props.get("pageProps", {})
        print("pageProps keys:", sorted(pprop.keys()))
        # 深挖找职位数据
        def walk(obj, path, depth=0):
            hits = []
            if depth > 6:
                return hits
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = str(k).lower()
                    if any(w in kl for w in ("job", "position", "recruit")):
                        sample = str(v)[:150] if not isinstance(v, (dict, list)) else (
                            "dict(%d)" % len(v) if isinstance(v, dict) else "list(%d)" % len(v))
                        hits.append((path + "/" + str(k), sample))
                    hits.extend(walk(v, path + "/" + str(k), depth + 1))
            elif isinstance(obj, list) and obj:
                hits.extend(walk(obj[0], path + "[0]", depth + 1))
            return hits
        for p, s in walk(nd, "ND")[:25]:
            print("  %s: %s" % (p, s))
        # Next.js 数据路由探测
        build = nd.get("buildId")
        if build:
            for dr in ["https://we.dji.com/_next/data/%s/zh-CN/list.json" % build,
                       "https://we.dji.com/_next/data/%s/zh-CN.json" % build]:
                try:
                    rd = c.get(dr, timeout=25)
                    print("GET %s -> %s ct=%s len=%s" % (
                        dr, rd.status_code, rd.headers.get("content-type"),
                        len(rd.text)))
                    if rd.status_code == 200 and "json" in (rd.headers.get("content-type") or ""):
                        print("  body[:300]:", rd.text[:300].replace("\n", " "))
                except Exception as e:
                    print("GET %s -> ERROR %s" % (dr, e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
