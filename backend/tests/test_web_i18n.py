"""多语言看板前端契约测试（§12.6 P5 ⑤）：词典完整性 + 语言码与后端一致 + 接线。

前端没有自动化测试框架（见 test_pwa_assets.py 的说明），沿用"后端 pytest 校验前端静态
资产内容契约"的折中：**漏翻译**与**前后端语言码漂移**这两类问题无法靠 `next build` 发现
（缺 key 只会静默显示 key 本身），但都适合做静态断言。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.i18n import SUPPORTED_LANGS

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
I18N = FRONTEND / "app" / "i18n.js"
LAYOUT = FRONTEND / "app" / "layout.js"
LANG_SWITCH = FRONTEND / "app" / "lang-switch.js"
INSIGHTS = FRONTEND / "app" / "insights" / "page.js"
CSS = FRONTEND / "app" / "globals.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dict_keys(source: str, name: str) -> set[str]:
    """提取 `const <name> = {...}` 的顶层 key（约定缩进 2 空格）。"""
    start = source.index(f"const {name} = {{")
    end = source.index("\n};", start)
    return set(re.findall(r"^\s{2}([A-Za-z_]\w*):", source[start:end], re.M))


def test_frontend_dicts_cover_the_same_keys():
    source = _read(I18N)
    zh = _dict_keys(source, "ZH")
    en = _dict_keys(source, "EN")
    assert len(zh) > 40  # 词典确实被解析出来了（防正则失效导致的"空集合假绿"）
    assert zh == en, f"缺翻译的 key：{sorted(zh ^ en)}"


def test_frontend_langs_match_backend_supported_langs():
    """语言码只有一处事实来源：后端 i18n.SUPPORTED_LANGS。"""
    codes = re.findall(r'code:\s*"([^"]+)"', _read(I18N))
    assert tuple(codes) == tuple(SUPPORTED_LANGS)


def test_unknown_lang_falls_back_not_throws():
    # 注释里会解释"为什么不自动协商"，故先剔除整行注释再断言
    code = "\n".join(
        line for line in _read(I18N).splitlines() if not line.strip().startswith("//")
    )
    # 与后端同口径：不认识的语言回退默认，不做自动协商（navigator.language）
    assert "hasOwnProperty.call(DICTS, value)" in code
    assert "navigator.language" not in code


def test_layout_renders_html_lang_from_cookie():
    source = _read(LAYOUT)
    assert 'from "next/headers"' in source and "await cookies()" in source
    assert "LANG_COOKIE" in source and "normalizeLang" in source
    assert "<html lang={lang}>" in source
    assert "<LangSwitch lang={lang} />" in source


def test_lang_switch_writes_cookie_then_reloads():
    source = _read(LANG_SWITCH)
    assert '"use client"' in source
    assert "document.cookie = langCookieValue(code)" in source
    # 只切 state 会造成"文案英文、<html lang> 中文"的错配，必须整页刷新
    assert "window.location.reload()" in source
    assert "if (code === current) return;" in source


def test_insights_page_consumes_seasonality_api_with_region_and_lang():
    source = _read(INSIGHTS)
    assert "/api/insights/seasonality?region=${region}&lang=${lang}" in source
    assert "readLangCookie()" in source

    css = _read(CSS)
    for selector in (".lang-switch", ".bar-track", ".bar.peak", ".bar.off"):
        assert selector in css