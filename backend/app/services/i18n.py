"""多语言展示标签（§12.6 P5 ⑤）：让洞察类报告的"枚举标签"按语言渲染。

**为什么这么做**
- 「全球多语言看板」的最小可用形态不是翻译整站文案（那是前端工程），而是让**后端报告里
  语言相关的那一小块**（区域名、季节标签、月份/季度名）可按 `lang` 渲染，报告主体
  （数值与结构）保持语言无关——前端在任何语言下都消费同一份结构化数据。
- 报告只回**结构化枚举**（`peak` / `off` / `cn` …），标签在这里集中翻译；这样新增语言
  只需加一个字典条目，不用碰业务逻辑。

**口径说明**
- 支持语言：`zh-CN`（默认）、`en`。API 层用 `SUPPORTED_LANGS` 显式拒绝其他值（422，
  与 `region` 校验同风格）；`labels()` 仍做**兜底回退**——供脚本/定时任务等非 API
  调用方使用，展示层不该因为一个语言串崩掉。
- 只翻译**枚举与固定提示语**；职位标题、城市名、技能标签等原始数据原样输出
  （那些是机器噪声，翻译只会引入失真）。

**取舍记录**
- 不引 i18n 框架 / 不接翻译平台：本项目的消费者只有两类（中文看板、英文看板），
  一个纯字典模块足够；上 gettext/Babel 属超前工程。
- 语言不走 `Accept-Language` 自动协商：看板要能"钉住"某个语言（分享链接给英文同事），
  显式参数 + 前端 cookie 记忆比隐式协商更可控。
"""

from __future__ import annotations

SUPPORTED_LANGS: tuple[str, ...] = ("zh-CN", "en")
DEFAULT_LANG: str = "zh-CN"

# 月份英文缩写（与中文"1 月"对齐；ISO 顺序，索引 0 = 1 月）
_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_LABELS: dict[str, dict] = {
    "zh-CN": {
        "region": {"all": "全部", "cn": "国内", "overseas": "海外"},
        "season": {"peak": "旺季", "shoulder": "平季", "off": "淡季"},
        "months": tuple(f"{m} 月" for m in range(1, 13)),
        "quarters": ("一季度", "二季度", "三季度", "四季度"),
    },
    "en": {
        "region": {"all": "All", "cn": "China", "overseas": "Overseas"},
        "season": {"peak": "peak", "shoulder": "shoulder", "off": "off-season"},
        "months": _EN_MONTHS,
        "quarters": ("Q1", "Q2", "Q3", "Q4"),
    },
}


def resolve_lang(lang: str | None) -> str:
    """把任意输入归一到受支持语言码；不支持的（含 None）回退默认语言，不抛异常。"""
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def labels(lang: str = DEFAULT_LANG) -> dict:
    """取某语言的标签表（含 region / season / months / quarters）；未知语言回退默认。"""
    return _LABELS[resolve_lang(lang)]


def month_label(lang: str, month: int) -> str:
    """月份标签：1~12 → "3 月" / "Mar"；越界原样回字符串（调用方保证 1~12）。"""
    months = labels(lang)["months"]
    if 1 <= month <= len(months):
        return months[month - 1]
    return str(month)


def quarter_label(lang: str, quarter: int) -> str:
    """季度标签：1~4 → "一季度" / "Q1"；越界回 "Q{n}"。"""
    quarters = labels(lang)["quarters"]
    if 1 <= quarter <= len(quarters):
        return quarters[quarter - 1]
    return f"Q{quarter}"