# -*- coding: utf-8 -*-
"""大疆收尾:从页面里找 orgId,试明文的 settings/jobs 接口。"""
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
        page = c.get(BASE + "/social-recruitment/dji/170070").text
        print("len(page) =", len(page))
        for pat in [r'orgId["\']?\s*[:=]\s*["\']?(\d+)', r'"orgId":"?(\d+)']:
            hits = sorted(set(re.findall(pat, page)))
            print("orgId hits:", hits[:10])
        # 内联脚本里找 siteId/orgId 上下文
        for m in list(re.finditer(r'.{80}(?:orgId|siteId).{80}', page))[:6]:
            print("ctx:", m.group(0).replace("\n", " ")[:170])
        # 用候选值试 settings/jobs
        for oid in ["170070"] + sorted(set(re.findall(r'orgId["\']?\s*[:=]\s*["\']?(\d+)', page)))[:3]:
            body = {"orgId": int(oid), "currentPage": 1, "pageSize": 5}
            rr = c.post(BASE + "/api/outer/ats-apply/website/settings/jobs",
                        json=body, timeout=25)
            frag = rr.text[:220].replace("\n", " ")
            print("settings/jobs orgId=%s -> %s | %s" % (oid, rr.status_code, frag))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
