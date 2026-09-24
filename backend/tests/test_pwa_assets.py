"""PWA / 移动端资产契约测试（§12.6 P5 ④）。

PWA 资产是纯前端静态件（manifest / service worker / 视口 / 响应式 CSS），后端测试里
做**内容契约校验**（存在性 + 关键字段 + 关键约束），避免只靠人工目测回归；真正的
"可安装 / 离线可用"行为需浏览器环境（Lighthouse / 手动装到主屏），属剩余条件（非代码）。
"""

from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_manifest_declares_installability_fields():
    text = _read("app/manifest.js")
    assert "export default function manifest" in text  # Next 元数据路由
    for key in ("name", "short_name", "start_url", "display", "theme_color", "background_color", "icons"):
        assert key in text, f"manifest 缺少 {key}"
    assert '"standalone"' in text  # 独立窗口（非浏览器标签页）
    assert "/icons/icon.svg" in text


def test_icon_asset_exists_and_is_svg():
    icon = (FRONTEND / "public/icons/icon.svg").read_text(encoding="utf-8")
    assert icon.lstrip().startswith("<svg")
    assert "viewBox" in icon
    assert "maskable" not in icon  # maskable 由 manifest 的 purpose 声明，不写进 SVG 本体


def test_service_worker_never_caches_api_and_has_lifecycle():
    sw = _read("public/sw.js")
    # 隐私 + 时效硬约束：/api/ 请求绝不缓存
    assert '"/api/"' in sw and "绝不缓存 API" in sw
    # 三个生命周期钩子齐备
    for hook in ('addEventListener("install"', 'addEventListener("activate"', 'addEventListener("fetch"'):
        assert hook in sw, f"service worker 缺少 {hook}"
    assert "navigate" in sw  # 导航请求 network-first 回退外壳
    assert "/_next/static" in sw  # 静态资源 stale-while-revalidate


def test_layout_wires_viewport_and_sw_registration():
    layout = _read("app/layout.js")
    assert "export const viewport" in layout
    assert "device-width" in layout
    assert "themeColor" in layout
    assert "viewports" not in layout  # Next 15 用 viewport（单数）导出
    assert "ServiceWorkerRegister" in layout
    assert "appleWebApp" in layout  # iOS 加主屏


def test_responsive_media_query_present():
    css = _read("app/globals.css")
    assert "@media (max-width: 640px)" in css
    assert "safe-area-inset-bottom" in css  # iPhone 底部安全区
    assert "min-height: 44px" in css  # 触控目标下限


def test_sw_register_component_is_client_and_guarded():
    text = _read("app/sw-register.js")
    assert text.lstrip().startswith('"use client"')
    assert '"serviceWorker" in navigator' in text  # 能力探测后才注册
    assert "catch" in text  # 注册失败静默（PWA 是增强项）