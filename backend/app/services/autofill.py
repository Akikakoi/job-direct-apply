"""半自动帮填（P3 §8 三档模式之二：semi_auto）。

合规边界（§4/§8，不做任何绕过）：
- 只自动填写"公开表单"的常见字段（姓名/邮箱/电话），从不自动点击提交；
- 浏览器以有头模式打开并保持，由用户人工核对后自行提交；
- 登录墙/验证码一律留给用户，不碰。

用户档案从 settings 读取（AUTOFILL_NAME / AUTOFILL_EMAIL / AUTOFILL_PHONE，
.env 配置；当前单用户产品形态）。

用法：POST /api/applications/{id}/autofill 后台线程拉起浏览器，接口立即返回。
"""

from __future__ import annotations

import threading

from app.core.config import settings


class AutofillNotConfigured(Exception):
    """用户档案未配置（name/email 全空时没有可填内容）。"""


# 启动 playwright 在独立线程跑（sync API 不能进 uvicorn 的 asyncio 线程）
def launch_autofill_thread(apply_url: str) -> threading.Thread:
    t = threading.Thread(target=_run_autofill, args=(apply_url,), daemon=True)
    t.start()
    return t


def _profile() -> dict:
    profile = {
        "name": settings.autofill_name.strip(),
        "email": settings.autofill_email.strip(),
        "phone": settings.autofill_phone.strip(),
    }
    if not profile["email"] and not profile["name"]:
        raise AutofillNotConfigured("AUTOFILL_NAME / AUTOFILL_EMAIL 未配置（backend/.env）")
    return profile


def _run_autofill(apply_url: str, keep_open: bool = True) -> list[str]:
    """打开页面并预填；keep_open=True（服务端模式）浏览器保持打开交给用户。"""
    from playwright.sync_api import sync_playwright

    profile = _profile()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.autofill_headless)
            page = browser.new_page()
            page.goto(apply_url, timeout=60000, wait_until="domcontentloaded")
            filled = _fill_common_fields(page, profile)
            print(f"[autofill] opened {apply_url}, filled={filled}; 人工核对后请手动提交")
            if not keep_open:
                browser.close()
                return filled
            # 不 close：浏览器保持打开交给用户。daemon 线程随进程退出。
            try:
                while browser.is_connected() and not page.is_closed():
                    page.wait_for_timeout(5000)
            except Exception:
                pass
            return filled
    except Exception as exc:  # 打不开/未装 chromium 都不中断主服务
        print(f"[autofill] failed: {type(exc).__name__}: {exc}")
        return []


def _fill_common_fields(page, profile: dict) -> list[str]:
    """启发式填公开表单：只碰 email/tel/name 类输入框，绝不碰提交按钮。"""
    filled: list[str] = []
    try:
        for locator in page.locator("input:visible").all():
            attrs = {
                (locator.get_attribute(k) or "").lower()
                for k in ("type", "name", "id", "placeholder", "autocomplete")
            }
            joined = " ".join(attrs)
            if "email" in joined and profile["email"]:
                locator.fill(profile["email"])
                filled.append("email")
            elif ("tel" in joined or "phone" in joined or "手机" in joined) and profile["phone"]:
                locator.fill(profile["phone"])
                filled.append("phone")
            elif (
                ("name" in joined and "email" not in joined and "file" not in joined)
                and profile["name"]
            ):
                locator.fill(profile["name"])
                filled.append("name")
    except Exception as exc:
        print(f"[autofill] partial fill error: {type(exc).__name__}: {exc}")
    return filled
