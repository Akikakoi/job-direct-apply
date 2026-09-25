"""登录页与令牌存储的前端契约测试（§4.3）。

前端没有自动化测试框架（见 test_pwa_assets.py / test_web_i18n.py 的说明），沿用"后端
pytest 校验前端静态资产内容契约"的折中。这里盯的是三类**靠 `next build` 发现不了**的问题：

1. 令牌键名/请求头写法与后端对不上（`Authorization: Bearer` + 两个 localStorage 键）；
2. 401 自愈链路缺失（不换令牌、或换了不重试、或换不到不跳登录）——写成静态断言，
   免得以后重构把这条链路悄悄改断；
3. 用户态接口漏接 authFetch（裸 fetch 不带令牌，登录后仍拿到匿名视角的数据）。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
APP = FRONTEND / "app"
AUTH = APP / "auth.js"
LOGIN = APP / "login" / "page.js"
AUTH_BAR = APP / "auth-bar.js"
LAYOUT = APP / "layout.js"
PAGE = APP / "page.js"
CSS = APP / "globals.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_auth_module_stores_the_two_token_keys():
    source = _read(AUTH)
    assert '"use client"' in source
    assert 'ACCESS_KEY = "access_token"' in source
    assert 'REFRESH_KEY = "refresh_token"' in source
    assert "localStorage" in source
    # 服务端渲染阶段不能碰 localStorage（typeof window 守卫）
    assert 'typeof window === "undefined"' in source
    assert "clearTokens" in source and "isLoggedIn" in source


def test_auth_fetch_attaches_bearer_header():
    source = _read(AUTH)
    # 后端只认 Authorization: Bearer（见 app/main.py 的 _bearer_token）
    assert "`Bearer ${token}`" in source
    assert 'headers.set("Authorization"' in source


def test_auth_fetch_refreshes_once_then_retries_then_redirects():
    source = _read(AUTH)
    assert '"/api/auth/refresh"' in source
    assert "refresh_token: refresh" in source  # 刷新请求体字段名与后端 RefreshRequest 一致
    assert "resp.status !== 401" in source  # 只在 401 时走自愈链路
    assert "if (await refreshTokens())" in source  # 换到令牌后必须重试原请求
    assert "let refreshing = null;" in source  # 并发 401 只换一次
    # 换不到令牌 → 清令牌跳登录页并带 next 回跳
    assert "LOGIN_PATH = \"/login\"" in source
    assert "?next=${encodeURIComponent(next)}" in source
    assert "clearTokens();" in source
    # 登录态自检（/api/auth/me）不该被带去登录页
    assert "redirectOnFail = true" in source


def test_login_page_posts_to_auth_endpoints_and_stores_tokens():
    source = _read(LOGIN)
    assert '"use client"' in source
    # 登录/注册同表单双 Tab，路径由 mode 拼出（mode 取值必须与后端路由同名）
    assert "`/api/auth/${mode}`" in source
    assert 'setMode("login")' in source and 'setMode("register")' in source
    assert 'id="email"' in source and 'id="password"' in source
    assert 'autoComplete={mode === "login" ? "current-password" : "new-password"}' in source
    assert "setTokens(body?.data)" in source
    assert "window.location.href = next" in source
    # 回跳地址只接受站内相对路径（挡协议相对 URL 的开放重定向）
    assert 'target.startsWith("/") && !target.startsWith("//")' in source
    # 协议入口不可少（§14 ①：注册/登录即表示同意）
    assert 'href="/legal/terms"' in source and 'href="/legal/privacy"' in source


def test_auth_bar_shows_login_or_logout_and_is_mounted_in_layout():
    source = _read(AUTH_BAR)
    assert '"use client"' in source
    assert "isLoggedIn()" in source
    assert 'href="/login"' in source  # 未登录给登录入口
    assert "退出" in source and "clearTokens()" in source
    # 令牌在 localStorage，服务端渲染必须先渲染空，避免 hydration 不一致
    assert "if (!mounted) return null;" in source
    assert 'authFetch("/api/auth/me", { redirectOnFail: false })' in source

    layout = _read(LAYOUT)
    assert 'import AuthBar from "./auth-bar"' in layout
    assert "<AuthBar />" in layout


def test_home_page_uses_auth_fetch_for_user_scoped_apis():
    source = _read(PAGE)
    assert 'import { authFetch } from "./auth"' in source
    # 裸 fetch 不带令牌：登录后仍会拿到匿名视角的数据，这里必须清干净
    assert "fetch(" not in source
    for endpoint in (
        "/api/applications?user_id=1&limit=50",
        "/api/reminders?user_id=1",
        "/api/resumes/${resumeId}",
        "/api/recommend?resume_id=${resumeId}&limit=50&region=${regionValue}",
        "/api/applications",
        "/api/resumes",
        "/api/legal/policies",
        "/api/applications/${a.id}/autofill",
    ):
        # 结合上面的"无裸 fetch"：这些接口字面量存在即只能由 authFetch 调用
        assert f'"{endpoint}"' in source or f"`{endpoint}`" in source, (
            f"用户态接口调用缺失：{endpoint}"
        )


def test_auth_bar_and_login_form_styles_present():
    css = _read(CSS)
    for selector in (".auth-bar", ".auth-form input", '.auth-form button[type="submit"]'):
        assert selector in css, f"样式缺失：{selector}"