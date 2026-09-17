# -*- coding: utf-8 -*-
"""大疆第五轮:从 __NEXT_DATA__ 提取站内链接 + 从 JS bundle 挖 stormsend API 完整路径。"""
import json
import re
import sys

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}


def main():
    with httpx.Client(headers=HEADERS, follow_redirects=True) as c:
        r = c.get("https://we.dji.com/zh-CN", timeout=25)
        html = r.text
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
                      html, re.S)
        nd = json.loads(m.group(1))
        # 1) 页面里所有 href / buttonLink
        print("--- href links in html ---")
        for h in sorted(set(re.findall(r'href="(/[^"]+|https?://[^"]+)"', html)))[:30]:
            print("  ", h)
        pprop = nd["props"]["pageProps"]

        def dump_links(obj, path="", depth=0):
            if depth > 7:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if str(k).lower() in ("link", "buttonlink", "url", "href") and isinstance(v, str):
                        print("  %s/%s = %s" % (path, k, v))
                    dump_links(v, path + "/" + str(k), depth + 1)
            elif isinstance(obj, list):
                for i, v in enumerate(obj[:4]):
                    dump_links(v, path + "[%d]" % i, depth + 1)
        print("--- links in __NEXT_DATA__ ---")
        dump_links(nd)
        # 2) 抓所有 bundle,找 stormsend / position 相关字符串上下文
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        urls = []
        for s in srcs:
            if s.startswith("//"):
                urls.append("https:" + s)
            elif s.startswith("http"):
                urls.append(s)
        print("--- bundle scan (%d scripts) ---" % len(urls))
        pat = re.compile(r'.{60}(?:stormsend[^"\']{0,120}|/api/[a-zA-Z0-9_\-/\[\]{}.:$]{2,80}).{20}',
                         re.S)
        pos_pat = re.compile(r'.{40}["\'](/[a-zA-Z0-9_\-]*position[a-zA-Z0-9_\-/]*)["\'].{10}', re.S)
        for u in urls:
            if not u.endswith(".js"):
                continue
            try:
                rb = c.get(u, timeout=30)
                if rb.status_code != 200:
                    continue
            except Exception:
                continue
            js = rb.text
            for mm in list(pat.finditer(js))[:8]:
                frag = mm.group(0).replace("\n", " ")
                print("[%s] %s" % (u[-45:], frag[:170]))
            for mm in list(pos_pat.finditer(js))[:6]:
                frag = mm.group(0).replace("\n", " ")
                print("[%s POS] %s" % (u[-45:], frag[:150]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
