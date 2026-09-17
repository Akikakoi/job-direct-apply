"""城市名规范化（P1 挂账项）。

问题：/api/jobs?city=杭州 返回 0，因为库内存的是"杭州市"（国内源带"市"后缀），
海外源还有 "New York, NY"、"Menlo Park, CA; New York, NY"（多城市分号拼接）、
"NYC"/"Bangalore" 这类别名。

方案（查询侧 contains 匹配）：
- 查询词先做小别名归一（NYC→New York、Bangalore→Bengaluru）；
- 再去掉结尾"市"字，得到匹配基词；
- 用 lower(city) LIKE '%基词%' 匹配，天然覆盖：
  - "杭州" ⊂ "杭州市"
  - "New York" ⊂ "New York, NY" 及多城市串 "Menlo Park, CA; New York, NY"
- LIKE 通配符（% _ \）转义，避免用户输入干扰匹配。

采集侧（collect.py）不做改写，保留原始 city 值以便追溯源数据。
"""

from __future__ import annotations

# 常见别名 → 标准写法（小写键）。按需补充，先覆盖实测库内出现的别名。
CITY_ALIASES: dict[str, list[str]] = {
    "nyc": ["new york"],
    "new york city": ["new york"],
    "bangalore": ["bengaluru"],
    "sf": ["san francisco"],
    "san fran": ["san francisco"],
}


def _escape_like(value: str) -> str:
    """转义 SQL LIKE 通配符，按 SQLite/PG 默认反斜杠转义（ESCAPE '\\'）。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def city_match_variants(city: str | None) -> list[str]:
    """把用户查询词展开为一组 LIKE 匹配基词（已转义、已小写）。

    例：
      "杭州"   -> ["杭州"]
      "杭州市" -> ["杭州"]
      "NYC"    -> ["new york", "nyc"]   # 别名展开 + 原词保留
      " Bangalore " -> ["bangalore", "bengaluru"]
      "北京"   -> ["北京"]
    """
    if not city:
        return []
    raw = city.strip()
    if not raw:
        return []

    # 候选集：原文 + 别名展开（别名展开本身也是标准写法，无需再去后缀）
    candidates = {raw}
    lowered = raw.lower()
    candidates.update(CITY_ALIASES.get(lowered, []))

    bases: set[str] = set()
    for cand in candidates:
        base = cand[:-1] if cand.endswith("市") and len(cand) > 1 else cand
        base = base.strip()
        if base:
            bases.add(base.lower())
    return sorted(_escape_like(b) for b in bases)
