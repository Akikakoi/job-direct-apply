"""规则抽取与校验（P2 §6 防幻觉层）。

extract_profile_rules: 纯正则/词典兜底抽取，不依赖任何外部服务，测试可离线跑。
validate_profile: LLM 结果与规则结果合并——LLM 缺失用规则填，数值冲突以规则为准并记 notes。
"""

from __future__ import annotations

import re

# 常见投递城市（与 jobs 库内高频城市对齐 + 远程）；按需补充
KNOWN_CITIES = [
    "北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "西安", "武汉",
    "苏州", "合肥", "长沙", "重庆", "天津", "郑州", "厦门", "福州", "青岛",
    "香港", "台北", "Singapore", "Bengaluru", "Bangalore", "New York",
    "San Francisco", "Seattle", "London", "Dublin", "Toronto", "Remote",
]
CITY_ALIASES_RULE = {"远程": "远程", "Remote": "远程", "NYC": "New York"}

DEGREE_PATTERNS: list[tuple[str, str]] = [
    (r"博士|ph\.?d", "phd"),
    (r"硕士|研究生|master", "master"),
    (r"本科|bachelor|学士", "bachelor"),
    (r"大专|专科|associate", "associate"),
]

_SALARY_K = r"(\d{1,3})\s*[kK千]?\s*[-~～至到]\s*(\d{1,3})\s*[kK千]"
_SALARY_SINGLE = r"(\d{1,3})\s*[kK千]"
_EXP = r"(\d{1,2})\s*\+?\s*年(?:以上)?(?:的)?(?:工作|开发|相关)?经验"
_EXP_EN = r"(\d{1,2})\s*\+?\s*years?"

# 与英文常用词同形的 ASCII 技能标签：仅靠 \b 匹配会在英文 JD 散文里大面积误命中
# （实测 greenhouse 853 条 JD 中 "go" 命中 307 条，但只有 ~94 条真在说 Go 语言，
# 其余是 go-to-market / go live / go beyond）。这类 token 改为"首字母大写出现"
# 才算命中——技术写作惯例如此；负向断言排除 Go-to / Go live 两个固定搭配。
_AMBIGUOUS_ASCII: dict[str, re.Pattern[str]] = {
    "go": re.compile(r"\bGo(?!(?i:[\s-](?:to|live)\b))"),
}


def detect_lang(text: str) -> str:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return "zh" if cjk >= 10 else "en"


def scan_skills(text: str, alias_map: dict[str, str]) -> list[str]:
    """词典扫描：所有在文本中出现的标准标签/别名。

    ASCII 词用 \b 词边界匹配（避免 "go" 误伤 django/mongo 这类子串），
    中文词无边界概念，用子串；歧义 token（见 _AMBIGUOUS_ASCII）走大写匹配，
    故低写成 "go 语言" 的简历需写成 Go/golang 才能命中（golang 是别名，仍可命中）。
    """
    lowered = text.lower()
    hits: set[str] = set()
    for token, canonical in alias_map.items():
        if not token:
            continue
        if not token.isascii():
            if token in lowered:
                hits.add(canonical)
            continue
        pattern = _AMBIGUOUS_ASCII.get(token)
        if pattern is not None:
            if pattern.search(text):  # 需用原文：大写判定在 lowered 上会失效
                hits.add(canonical)
        elif re.search(rf"\b{re.escape(token)}\b", lowered):
            hits.add(canonical)
    return sorted(hits)


# ---------- 教育/项目/实习结构化抽取（LLM 不可用时的兜底：章节切分 + 正则） ----------

# 章节标题白名单：命中即切段；"_other" 仅作边界（终止当前节），不产出字段
_SECTION_KEYS: list[tuple[str, str]] = [
    ("education", r"教育经历|教育背景|学习经历"),
    ("projects", r"项目经历|项目经验|实践经历|项目实践"),
    ("internships", r"实习经历|实习经验|工作经历|工作经验|职业经历"),
    ("_other", r"专业技能|技能特长|技能清单|自我评价|个人总结|荣誉奖项|获奖情况|"
               r"证书|语言能力|基本信息|联系方式|求职意向"),
]
_HEADER_RE = re.compile("|".join(f"(?:{p})" for _, p in _SECTION_KEYS))

# 院校：CJK 前缀 + 大学/学院（兼容"XX大学XX学院"两级），排除"大学生/大学时"等误伤
_SCHOOL_RE = re.compile(
    r"([\u4e00-\u9fff]{2,10}(?:大学|学院)(?:[\u4e00-\u9fff]{2,10}学院)?)"
    r"(?!生|时|期间|里|中|的|毕|校)"
)
# 「专业 + 分隔符 + 学历」写法，如"计算机科学与技术 · 本科"
_MAJOR_DEGREE_RE = re.compile(
    r"([\u4e00-\u9fff]{2,20})\s*[·•・|｜/]\s*(本科|学士|硕士|研究生|博士|大专|专科)"
)
# 时间区间：2024/09 - 至今（中英文起止写法）
_PERIOD_RE = re.compile(
    r"(\d{4}\s*[/.\-年]\s*\d{1,2}\s*月?)\s*[-~～—－至到]{1,2}\s*"
    r"(\d{4}\s*[/.\-年]\s*\d{1,2}\s*月?|至今|现在|present|now)",
    re.IGNORECASE,
)
# 项目技术栈行："技术栈：A · B · C"
_TECH_LINE_RE = re.compile(r"(?:技术栈|技术选型|使用技术)[:：]\s*(.+)")
_TECH_SPLIT_RE = re.compile(r"[·•・|｜,，、/]+")


def _degree_of(word: str) -> str | None:
    for pattern, degree in DEGREE_PATTERNS:
        if re.search(pattern, word, re.IGNORECASE):
            return degree
    return None


def _norm_period(value: str) -> str:
    """归一化时间写法：2024年9月 → 2024/9；保留"至今"等原词。"""
    v = re.sub(r"\s+", "", value).replace("年", "/").replace("月", "")
    return re.sub(r"[/.\-]+$", "", v) or value.strip()


def _split_sections(text: str) -> dict[str, list[str]]:
    """按白名单章节标题切分文本 → {字段名: 该节内容行}。标题行本身不保留。"""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if len(line) <= 12 and _HEADER_RE.fullmatch(line):
            current = None
            for key, pattern in _SECTION_KEYS:
                if re.fullmatch(pattern, line):
                    current = None if key == "_other" else key
                    break
            if current is not None:
                sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    return sections


def _extract_education(lines: list[str]) -> list[dict]:
    """教育经历：院校 + 专业/学历 + 时间区间 + 主修课程/荣誉。"""
    body = "\n".join(lines)
    schools = [m.group(1) for m in _SCHOOL_RE.finditer(body)]
    if not schools:
        return []
    entry: dict = {"school": schools[0]}
    if len(schools) > 1:
        entry["highlights"] = [f"其他院校：{'、'.join(schools[1:])}"]
    m = _MAJOR_DEGREE_RE.search(body)
    if m:
        entry["major"] = m.group(1)
        degree = _degree_of(m.group(2))
        if degree:
            entry["degree"] = degree
    m = _PERIOD_RE.search(body)
    if m:
        entry["start"], entry["end"] = _norm_period(m.group(1)), _norm_period(m.group(2))
    extra = [ln for ln in lines if re.match(r"^(主修课程|荣誉奖项|获奖情况|GPA)", ln)]
    if extra:
        entry["highlights"] = extra + entry.get("highlights", [])
    return [entry]


def _is_tech_continuation(line: str) -> bool:
    """技术栈跨行续写：整行仅由 ASCII 技术名与分隔符组成（PDF 换行常把技术栈拆两行）。"""
    return bool(line) and len(line) <= 120 and not re.search(r"[\u4e00-\u9fff。：:]", line)


def _extract_projects(lines: list[str]) -> list[dict]:
    """项目经历：以"技术栈"行为锚点切条目，上一行作项目名，其后文本作描述。"""
    anchors = [i for i, ln in enumerate(lines) if _TECH_LINE_RE.search(ln)]
    entries: list[dict] = []
    for pos, idx in enumerate(anchors):
        entry: dict = {}
        # 项目名取锚点上方最近的非空非链接行（PDF 文本常把仓库链接夹在中间）
        for j in range(idx - 1, max(-1, idx - 4), -1):
            cand = lines[j]
            if cand and not re.match(r"^https?://", cand):
                entry["name"] = cand[:80]
                break
        tech_raw = [_TECH_LINE_RE.search(lines[idx]).group(1)]
        desc_start = idx + 1
        while desc_start < len(lines) and _is_tech_continuation(lines[desc_start]):
            tech_raw.append(lines[desc_start])
            desc_start += 1
        # 逐段拆分（而非拼接后再拆）：跨行续写处无分隔符，拼接会把两行粘成一个技能名
        tech = [t.strip() for part in tech_raw for t in _TECH_SPLIT_RE.split(part) if t.strip()]
        if tech:
            entry["tech"] = tech
        end = anchors[pos + 1] - 1 if pos + 1 < len(anchors) else len(lines)
        desc = " ".join(lines[desc_start:max(desc_start, end)]).strip()
        if desc:
            entry["description"] = desc[:400]
        if entry:
            entries.append(entry)
    return entries


def _extract_internships(lines: list[str]) -> list[dict]:
    """实习/工作经历：按时间区间行切条目，其前两行依次作公司/职位。"""
    body = [ln for ln in lines if ln]
    if not body:
        return []
    periods = [i for i, ln in enumerate(body) if _PERIOD_RE.search(ln)]
    entries: list[dict] = []
    if not periods:  # 无时间锚点：整节作为单条，首行当公司
        entry: dict = {"company": body[0][:60]}
        if len(body) > 1:
            entry["title"] = body[1][:60]
        desc = " ".join(body[2:]).strip()
        if desc:
            entry["description"] = desc[:400]
        return [entry]
    for pos, idx in enumerate(periods):
        m = _PERIOD_RE.search(body[idx])
        entry = {"start": _norm_period(m.group(1)), "end": _norm_period(m.group(2))}
        head = body[max(0, idx - 2) : idx]
        if head:
            entry["company"] = head[0][:60]
        if len(head) > 1:
            entry["title"] = head[1][:60]
        end = periods[pos + 1] - 2 if pos + 1 < len(periods) else len(body)
        desc = " ".join(body[idx + 1 : max(idx + 1, end)]).strip()
        if desc:
            entry["description"] = desc[:400]
        entries.append(entry)
    return entries


def extract_profile_rules(text: str, alias_map: dict[str, str] | None = None) -> dict:
    """正则/词典兜底抽取。alias_map 不传则只做非 skills 字段。"""
    profile: dict = {}
    notes: list[str] = []

    # 经验年限：取全部匹配的最大值（中英文两种写法）
    years = [int(m) for m in re.findall(_EXP, text)] + [
        int(m) for m in re.findall(_EXP_EN, text, re.IGNORECASE)
    ]
    if years:
        profile["experience_years"] = max(y for y in years if 0 <= y <= 50)

    # 学历：按优先级取最高（博士>硕士>本科>大专）
    for pattern, degree in DEGREE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            profile["edu_degree"] = degree
            break

    # 薪资：优先区间，其次单值（单位 K/月）
    m = re.search(_SALARY_K, text)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        profile["salary_min"], profile["salary_max"] = lo, hi
    else:
        m = re.search(_SALARY_SINGLE, text)
        if m:
            v = int(m.group(1))
            profile["salary_min"] = profile["salary_max"] = v

    # 城市：已知城市表扫描，按文本出现位置排序（保持简历书写顺序）
    lowered = text.lower()
    hits: list[tuple[int, str]] = []
    for city in KNOWN_CITIES:
        idx = lowered.find(city.lower())
        if idx >= 0:
            canonical = CITY_ALIASES_RULE.get(city, city)
            hits.append((idx, canonical))
    cities: list[str] = []
    for _, canonical in sorted(hits):
        if canonical not in cities:
            cities.append(canonical)
    if cities:
        profile["cities"] = cities

    # 求职意向 / 目标职位
    m = re.search(r"(?:求职意向|目标职位|期望职位)[:：]\s*(\S{2,30})", text)
    if m:
        profile["target_role"] = m.group(1).strip()

    # 教育/项目/实习经历：章节切分 + 正则（LLM 兜底，尽力而为）
    sections = _split_sections(text)
    for key, extractor in (
        ("education", _extract_education),
        ("projects", _extract_projects),
        ("internships", _extract_internships),
    ):
        entries = extractor(sections.get(key) or [])
        if entries:
            profile[key] = entries

    profile["lang"] = detect_lang(text)
    if alias_map is not None:
        skills = scan_skills(text, alias_map)
        if skills:
            profile["skills"] = skills
    if notes:
        profile["notes"] = notes
    return profile


def validate_profile(llm: dict, rule: dict, notes: list[str]) -> dict:
    """LLM 结果叠加规则校验：缺失用规则填，数值冲突以规则为准。"""
    merged = dict(llm)

    for key in (
        "target_role",
        "cities",
        "industry",
        "edu_degree",
        "education",
        "projects",
        "internships",
    ):
        if not merged.get(key) and rule.get(key):
            merged[key] = rule[key]
            notes.append(f"{key}_from_rules")

    # 数值防幻觉：经验年限偏差 >3 年以规则为准
    llm_years, rule_years = merged.get("experience_years"), rule.get("experience_years")
    if llm_years is None and rule_years is not None:
        merged["experience_years"] = rule_years
        notes.append("experience_years_from_rules")
    elif llm_years is not None and rule_years is not None and abs(llm_years - rule_years) > 3:
        merged["experience_years"] = rule_years
        notes.append(f"experience_years_conflict_llm={llm_years}_use_rule={rule_years}")

    # 薪资：LLM 缺失或区间明显不合理（min>max 或超出 1~500K）时以规则为准
    llm_sal = (merged.get("salary_min"), merged.get("salary_max"))
    rule_sal = (rule.get("salary_min"), rule.get("salary_max"))
    llm_ok = all(isinstance(v, (int, float)) for v in llm_sal) and 0 < llm_sal[0] <= llm_sal[1] <= 500
    if not llm_ok and any(v is not None for v in rule_sal):
        merged["salary_min"], merged["salary_max"] = rule_sal
        notes.append("salary_from_rules")
    if not merged.get("skills") and rule.get("skills"):
        merged["skills"] = rule["skills"]
        notes.append("skills_from_rule_scan")

    return merged
