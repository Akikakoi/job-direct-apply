# -*- coding: utf-8 -*-
"""大疆第八轮(收尾):1) 验证 Moka 加密是否覆盖所有接口; 2) we.dji.com sitemap
与 /position/detail SSR 页面是否可解析。"""
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
BASE = "https://apply.careers.dji.com"


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as c:
        # 1) 其他 outer 接口是否也加密
        for ep, body in [
            ("/api/outer/ats-apply/website/jobs/recent", {"siteId": 170070, "pageSize": 5}),
            ("/api/outer/ats-apply/website/group-by-job", {"siteId": 170070}),
            ("/api/outer/ats-apply/website/settings/jobs", {"siteId": 170070, "currentPage": 1, "pageSize": 5}),
        ]:
            try:
                rr = c.post(BASE + ep, json=body, timeout=25)
                frag = rr.text[:180].replace("\n", " ")
                print("POST %s -> %s | %s" % (ep, rr.status_code, frag))
            except Exception as e:
                print("POST %s -> ERROR %s" % (ep, e))
        # 2) we.dji.com sitemap
        print()
        for sm in ["https://we.dji.com/sitemap.xml",
                   "https://we.dji.com/zh-CN/sitemap.xml"]:
            try:
                rs = c.get(sm, timeout=25)
                print("GET %s -> %s ct=%s len=%s" % (sm, rs.status_code,
                                                     rs.headers.get("content-type"),
                                                     len(rs.text)))
                if rs.status_code == 200 and "xml" in (rs.headers.get("content-type") or ""):
                    print("  head[:300]:", rs.text[:300].replace("\n", " "))
            except Exception as e:
                print("GET %s -> ERROR %s" % (sm, e))
        # 3) position detail 页面是否 SSR(从 Google 缓存拿不到,直接猜路由看返回)
        for u in ["https://we.dji.com/zh-CN/position/detail/1",
                  "https://we.dji.com/zh-CN/position/list"]:
            try:
                rp = c.get(u, timeout=25)
                has_next_data = "__NEXT_DATA__" in rp.text
                # 看看 NEXT_DATA 的 page 路由
                m = re.search(r'"page":"([^"]+)"', rp.text)
                print("GET %s -> %s len=%s next_data=%s page=%s" % (
                    u, rp.status_code, len(rp.text), has_next_data,
                    m.group(1) if m else "-"))
                # 404 页面通常较短;有职位内容的页面应包含 requirement 类字段
                for kw in ["职位描述", "jobDescription", "positionName", "requirement"]:
                    if kw in rp.text:
                        print("   contains:", kw)
            except Exception as e:
                print("GET %s -> ERROR %s" % (u, e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
