"""官网兜底适配器（OfficialSiteAdapter）——规则化解析实现。

P1 定稿（§5.3）：网易已评估并落地为 NeteaseAdapter（本模块的基类，提供 robots 校验与
"默认允许 + 低频"约定）；大疆经实测走 Moka、接口整体加密，主动解密属绕过反爬，休眠。
其余站点逐站评估后再接，遵守：低频、白名单、不绕反爬。

**本轮把骨架补成可用实现**，三种模式（按可信度从高到低，纯数据驱动、不执行任何代码）：

1. `json_ld`（默认，自动识别）：读页面里的 `application/ld+json`，找 `@type: JobPosting`
   （含 `@graph`）。这是搜索引擎强推的结构化数据，官网为了 SEO 大多会挂——比猜 DOM 稳得多；
2. `json`（SPA 常见）：按 `script_id` 取页面内嵌 JSON（`__NEXT_DATA__` 一类），再用
   `json_path`（点号路径）定位职位数组 + `fields` 声明字段映射；
3. `regex`（最后手段，逐字段正则）：页面没有结构化数据时才用，**脆弱**，字段缺失一律留空
   而不是瞎猜（宁可少字段，不可错字段——错字段会污染匹配与推荐）。

规则放在 `company.fetch_policy["html_rules"]`（JSON，纯数据）而不是新加一列：
这是"怎么抓这个站"的策略，与 interval_min 同属采集配置；真要独立成列时再迁移。

规则示例（SPA）：

    fetch_policy = {
        "interval_min": 720,
        "html_rules": {
            "list_url": "https://example.com/api/jobs?page={page}",
            "max_pages": 3,
            "json": {"script_id": "__NEXT_DATA__", "json_path": "props.pageProps.jobs"},
            "fields": {"title": "name", "city": "location.city", "url": "applyUrl"},
            "external_id_from": "url",
        },
    }

安全与合规：`discover` 先做 robots 校验（不允许 → `RobotsDisallowed`，不重试不绕过）；
每次采集只按 `official_site_interval_min`（默认 720 分钟）触发；不解析加密内容、不做反爬绕过。
"""

from __future__ import annotations

import html as html_mod
import json
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.adapters.base import AtsAdapter, NormalizedJob, RawJob
from app.models import Company

RULES_KEY = "html_rules"
DEFAULT_MAX_PAGES = 5

# 规范字段（与 §3.2 对齐）；规则/JSON 模式里用这些名字声明映射
CANONICAL_FIELDS = (
    "title", "url", "city", "description", "publish_date",
    "salary_min", "salary_max", "salary_currency", "experience_min", "degree_req", "external_id",
)


class OfficialSiteError(Exception):
    """官网兜底解析的基类异常。"""


class OfficialSiteNotConfigured(OfficialSiteError):
    """站点没有可用规则、页面也找不到结构化数据——属于"还没评估"，不是崩溃。"""


class RobotsDisallowed(OfficialSiteError):
    """robots.txt 明确不允许抓取该路径。"""


# ---------- robots（P1 既有口径） ----------


def parse_robots(text: str, url: str, ua: str = "*") -> bool:
    """判断 url 是否被 robots.txt 允许。纯解析，不触网，便于测试。"""
    from urllib.robotparser import RobotFileParser

    rp = RobotFileParser()
    rp.parse((text or "").splitlines())
    return rp.can_fetch(ua, url)


def robots_allowed(client, base_url: str, target_url: str, ua: str) -> bool:
    """同步拉取 robots.txt 并判断；取不到（4xx/5xx）视为允许，但仍遵守低频策略。"""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = client.get(robots_url)
    except Exception:  # 网络问题不当作"禁止"（与既有 async 版同口径）
        return True
    if resp.status_code >= 400:
        return True
    return parse_robots(resp.text, target_url, ua)


# ---------- HTML 工具（标准库，不引 bs4/lxml） ----------


def html_to_text(raw: str | None) -> str:
    """真实 HTML（未转义）→ 纯文本：**先剥标签再 unescape**。

    注意与 greenhouse 的 `content_to_text` 不同：那边是实体转义过的字符串
    （`&lt;div&gt;`），必须先 unescape 再剥标签；顺序反了要么整段 HTML 原样入库
    （greenhouse 的坑），要么剥出带标签的文本（本轮 JSON-LD 实测到的坑）。
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def escaped_to_text(raw: str | None) -> str:
    """**实体转义过**的字符串 → 纯文本：先 unescape 再剥标签。

    JSON-LD 里的 `description` 就是这种形态（JS 字符串里写着 `&lt;div&gt;`），
    按真实 HTML 的顺序处理会剥不出标签反而把实体还原成标记。
    """
    text = html_mod.unescape(raw or "")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class _ScriptCollector(HTMLParser):
    """收集 `<script>` 内容（按 type 分流：ld+json / 普通 JSON / 指定 id）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        self._current = {"id": attr.get("id", ""), "type": attr.get("type", ""), "text": []}

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._current is not None:
            self._current["text"] = "".join(self._current["text"])
            self.scripts.append(self._current)
            self._current = None


def scripts_in(html: str) -> list[dict]:
    collector = _ScriptCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # 畸形 HTML 不阻塞整批（解析器已尽量容错）
        pass
    return collector.scripts


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def json_ld_blocks(html: str) -> list:
    blocks = []
    for script in scripts_in(html):
        if "ld+json" not in (script["type"] or "").lower():
            continue
        data = _loads(script["text"])
        if data is not None:
            blocks.append(data)
    return blocks


def find_job_postings(blocks: list) -> list[dict]:
    """在 JSON-LD 里递归找 `@type: JobPosting`（含 @graph / 数组嵌套）。"""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        types = node.get("@type")
        type_list = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "jobposting" for t in type_list if t):
            found.append(node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value)

    walk(blocks)
    return found


def get_path(obj, path: str | None):
    """点号路径取值（`props.pageProps.jobs` / `items.0.name`）；不存在返回 None。"""
    if not path:
        return obj
    current = obj
    for part in str(path).split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def regex_fields(html: str, specs: dict) -> dict:
    """逐字段正则抽取；`group` 缺省 1、默认 DOTALL+IGNORECASE。命中不到就不写该字段。

    只回**原始命中串**，文本化（剥标签/还原实体）统一在 `normalize_raw` 按来源做——
    否则同一字段在两条路径上被处理两次，迟早对不上。
    """
    out: dict = {}
    for name, spec in (specs or {}).items():
        if not isinstance(spec, dict) or not spec.get("regex"):
            continue
        flags = 0 if spec.get("flags") == "none" else (re.S | re.I)
        try:
            match = re.search(spec["regex"], html, flags)
        except re.error:
            continue  # 规则写错只影响该字段，不让整轮采集挂掉
        if not match:
            continue
        try:
            value = match.group(int(spec.get("group", 1)))
        except (IndexError, ValueError):
            continue
        if value is not None:
            out[name] = str(value).strip()
    return out


def _salary_from_json_ld(value) -> dict:
    """JSON-LD baseSalary → (min, max, currency)；结构不对就整体留空。"""
    if not isinstance(value, dict):
        return {}
    amount = value.get("value")
    if isinstance(amount, dict):
        low, high = amount.get("minValue"), amount.get("maxValue")
        if low is None and high is None:
            low = high = amount.get("value")
    else:
        low = high = amount
    try:
        low_v = float(low) if low is not None else None
        high_v = float(high) if high is not None else None
    except (TypeError, ValueError):
        return {}
    if low_v is None and high_v is None:
        return {}
    return {
        "salary_min": low_v,
        "salary_max": high_v,
        "salary_currency": value.get("currency") or None,
    }


def _city_from_json_ld(location) -> str | None:
    items = location if isinstance(location, list) else [location]
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        address = item.get("address") if isinstance(item.get("address"), dict) else item
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            value = address.get(key) if isinstance(address, dict) else None
            if isinstance(value, dict):
                value = value.get("name")
            if value:
                parts.append(str(value))
    return ", ".join(dict.fromkeys(parts)) or None


def _external_id_from_url(url: str) -> str:
    """从详情链接里抠 id：查询串的 id/jobId/gh_jid，否则最后一段路径。"""
    parsed = urlparse(url or "")
    query = dict(re.findall(r"([^=&]+)=([^&]*)", parsed.query))
    for key in ("id", "jobId", "job_id", "gh_jid", "positionId"):
        if query.get(key):
            return str(query[key])
    tail = [seg for seg in parsed.path.split("/") if seg]
    return tail[-1] if tail else ""


def _to_date(value) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        try:
            return date.fromisoformat(match.group(0))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class OfficialSiteAdapter(AtsAdapter):
    """规则化官网适配器（模式与规则见模块 docstring）。"""

    ats_type = "official_site"

    # ---------- 规则 ----------

    def rules(self, company: Company) -> dict:
        policy = company.fetch_policy if isinstance(company.fetch_policy, dict) else {}
        rules = policy.get(RULES_KEY) or {}
        return rules if isinstance(rules, dict) else {}

    def _list_url(self, company: Company, rules: dict) -> str:
        url = rules.get("list_url") or company.feed_url or company.site_url
        if not url:
            raise OfficialSiteNotConfigured(f"{company.slug} 未配置 list_url（html_rules）")
        return str(url)

    # ---------- discover ----------

    def discover(self, company: Company) -> list[RawJob]:
        rules = self.rules(company)
        if not rules:
            # 未配规则 = 站点还没评估（§5.3）：直接报错，连页面都不抓
            raise OfficialSiteNotConfigured(
                f"{company.slug} 未配置 fetch_policy.{RULES_KEY}：先评估站点结构再开采集（§5.3）"
            )
        list_url = self._list_url(company, rules)
        base = f"{urlparse(list_url).scheme}://{urlparse(list_url).netloc}"
        if not robots_allowed(self._client, base, list_url, "*"):
            raise RobotsDisallowed(f"robots.txt 不允许抓取 {list_url}（不绕过，站点可标记休眠）")

        # 翻页能力才配得上多页默认：带 {page} 占位或配了 next_page 规则 → 默认翻 5 页
        paged = "{page}" in list_url or bool(rules.get("next_page"))
        max_pages = int(rules.get("max_pages") or (DEFAULT_MAX_PAGES if paged else 1))
        raws: list[RawJob] = []
        url = list_url
        page = int(rules.get("page_start") or 1)
        for _ in range(max(1, max_pages)):
            target = url.replace("{page}", str(page)) if "{page}" in url else url
            resp = self._client.get(target)
            resp.raise_for_status()
            html = resp.text or ""
            items = self._extract_items(company, rules, html, target)
            for item in items:
                item.setdefault("page_url", target)
                raws.append(RawJob(payload=item, company_slug=company.slug, feed_url=target))
            if "{page}" not in list_url:
                nxt = self._next_page(html, rules, target)
                if not nxt or nxt == target:
                    break
                url = nxt
                continue
            if not items:
                break
            page += 1
        if not raws:
            raise OfficialSiteNotConfigured(
                f"{company.slug} 页面没有可解析的职位（既无 JSON-LD JobPosting，也未配置 json/regex 规则）"
            )
        return raws

    def _next_page(self, html: str, rules: dict, current_url: str) -> str | None:
        spec = rules.get("next_page")
        if not spec:
            return None
        pattern = spec.get("regex") if isinstance(spec, dict) else spec
        if not pattern:
            return None
        match = re.search(pattern, html, re.S | re.I)
        if not match:
            return None
        href = match.group(int(spec.get("group", 1)) if isinstance(spec, dict) else 1)
        return urljoin(current_url, href.strip())

    def _extract_items(self, company: Company, rules: dict, html: str, page_url: str) -> list[dict]:
        """三种模式按可信度依次尝试；都不成返回空列表（由调用方决定是"翻页结束"还是"没配规则"）。"""
        postings = find_job_postings(json_ld_blocks(html))
        if postings:
            return [self._from_json_ld(p, page_url) for p in postings]

        json_spec = rules.get("json")
        if isinstance(json_spec, dict):
            items = self._from_embedded_json(html, json_spec, rules)
            if items:
                return items

        if rules.get("fields"):
            found = regex_fields(html, rules.get("fields"))
            if found:
                return [self._from_rules(found, rules, page_url)]

        return []

    def _from_embedded_json(self, html: str, spec: dict, rules: dict) -> list[dict]:
        script_id = spec.get("script_id")
        script_type = spec.get("script_type") or "application/json"
        data = None
        for script in scripts_in(html):
            if script_id and script["id"] != script_id:
                continue
            if not script_id and script_type not in (script["type"] or ""):
                continue
            data = _loads(script["text"])
            if data is not None:
                break
        if data is None:
            return []
        node = get_path(data, spec.get("json_path"))
        if isinstance(node, dict):
            node = [node]
        if not isinstance(node, list):
            return []
        fields = spec.get("fields") or rules.get("fields") or {}
        out: list[dict] = []
        for entry in node:
            if not isinstance(entry, dict):
                continue
            mapped = {name: get_path(entry, path) for name, path in fields.items()}
            mapped = {k: v for k, v in mapped.items() if v not in (None, "")}
            mapped["raw_type"] = "json"  # 内嵌 JSON 来源（描述按转义串处理）
            out.append(mapped)
        return out

    def _from_json_ld(self, posting: dict, page_url: str) -> dict:
        identifier = posting.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value")
        item = {
            "title": posting.get("title"),
            "url": posting.get("url") or posting.get("sameAs") or page_url,
            "city": _city_from_json_ld(posting.get("jobLocation")),
            "description": posting.get("description"),
            "publish_date": posting.get("datePosted"),
            "external_id": identifier,
            "raw_type": "json_ld",
        }
        item.update(_salary_from_json_ld(posting.get("baseSalary")))
        return {k: v for k, v in item.items() if v not in (None, "")}

    def _from_rules(self, found: dict, rules: dict, page_url: str) -> dict:
        item = {k: v for k, v in found.items() if k in CANONICAL_FIELDS}
        item.setdefault("url", page_url)
        item["raw_type"] = "rules"
        return item

    # ---------- normalize ----------

    def normalize_raw(self, raw: RawJob) -> NormalizedJob:
        item = raw.payload
        page_url = str(item.get("page_url") or raw.feed_url or "")
        apply_url = str(item.get("url") or page_url)
        if apply_url.startswith("/"):
            apply_url = urljoin(page_url, apply_url)

        external_id = str(item.get("external_id") or "").strip()
        if not external_id or item.get("external_id_from") == "url":
            external_id = _external_id_from_url(apply_url) or external_id
        if not external_id:
            # 没有稳定 id 时用链接兜底（同一职位同链接 → 仍可幂等 upsert）
            external_id = apply_url or str(item.get("title") or "")

        description = item.get("description")
        if description:
            # JSON-LD / 内嵌 JSON 里的描述是**实体转义串**，正则模式拿到的是真实 HTML
            escaped = item.get("raw_type") in ("json_ld", "json")
            description = escaped_to_text(description) if escaped else html_to_text(description)
        return NormalizedJob(
            external_id=external_id,
            title=str(item.get("title") or "").strip(),
            city=(str(item["city"]).strip() or None) if item.get("city") else None,
            skills=[],  # 采集侧统一打标签（词典优先 + LLM 兜底，§12.7 #7）
            experience_min=item.get("experience_min"),
            degree_req=item.get("degree_req"),
            salary_min=item.get("salary_min"),
            salary_max=item.get("salary_max"),
            salary_currency=item.get("salary_currency"),
            description=description or None,
            apply_url=apply_url,
            source=self.ats_type,
            publish_date=_to_date(item.get("publish_date")),
        )
