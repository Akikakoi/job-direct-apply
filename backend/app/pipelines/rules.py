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


def detect_lang(text: str) -> str:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return "zh" if cjk >= 10 else "en"


def scan_skills(text: str, alias_map: dict[str, str]) -> list[str]:
    """词典扫描：所有在文本中出现的标准标签/别名。

    ASCII 词用 \b 词边界匹配（避免 "go" 误伤 django/mongo 这类子串），
    中文词无边界概念，用子串。
    """
    lowered = text.lower()
    hits: set[str] = set()
    for token, canonical in alias_map.items():
        if not token:
            continue
        if token.isascii():
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                hits.add(canonical)
        elif token in lowered:
            hits.add(canonical)
    return sorted(hits)


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

    for key in ("target_role", "cities", "industry", "edu_degree"):
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
