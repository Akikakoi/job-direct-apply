# -*- coding: utf-8 -*-
"""解出网易 SPA 的 chunk 文件名映射,找 job-detail 路由参数形式。"""
import re
import sys

import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def main():
    c = httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
    js = c.get("https://hr.163.com/static/js/commons.c65656b8.chunk.js").text
    # webpack runtime: n.u = e => "static/js/" + e + "." + {...}[e] + ".chunk.js"
    m = re.search(r'n\.u\s*=\s*e\s*=>\s*[^;]{0,1200}chunk\.js', js, re.S)
    if not m:
        print("no chunk-name map found")
        return
    seg = m.group(0)
    # 找映射表 {...}
    hm = re.search(r'\{(\d+:"[a-f0-9]{8}"[^}]*)\}', seg)
    if not hm:
        print("map segment:", seg[:400])
        return
    entries = dict(re.findall(r'(\d+):"([a-f0-9]{8})"', hm.group(1)))
    print("chunk entries:", len(entries), "e.g.", list(entries.items())[:8])
    for cid in ("28", "2", "3", "4"):
        if cid in entries:
            u = "https://hr.163.com/static/js/%s.%s.chunk.js" % (cid, entries[cid])
            r = c.get(u)
            if r.status_code != 200:
                print(cid, "->", r.status_code)
                continue
            cjs = r.text
            hits = []
            for mm in list(re.finditer(r"job-detail", cjs))[:8]:
                s0 = max(0, mm.start() - 120)
                hits.append(cjs[s0:mm.end() + 120].replace("\n", " "))
            print("chunk %s (%dB) job-detail ctx:" % (cid, len(cjs)))
            for h in hits[:5]:
                print("   ...", h[:240])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
