"use client";

import { useEffect, useState } from "react";

import { authFetch, clearTokens, isLoggedIn } from "./auth";

// 账号状态条（§4.3）：固定左上角，未登录给登录入口，已登录显示邮箱 + 退出。
// 写法对齐 lang-switch.js：**登录态只在客户端可知**（令牌在 localStorage），
// 服务端渲染阶段一律先输出空（mounted=false），避免 hydration 前后 DOM 不一致。
export default function AuthBar() {
  const [mounted, setMounted] = useState(false);
  const [email, setEmail] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      if (!isLoggedIn()) {
        if (alive) setMounted(true);
        return;
      }
      try {
        // 登录态自检：未登录返回 401 是正常答案，不该被 authFetch 带去登录页
        const resp = await authFetch("/api/auth/me", { redirectOnFail: false });
        const data = resp.ok ? (await resp.json())?.data : null;
        if (alive) setEmail(data?.email || "");
      } catch {
        /* 取不到邮箱就只显示"退出"，不影响其他功能 */
      }
      if (alive) setMounted(true);
    })();
    return () => {
      alive = false;
    };
  }, []);

  function logout() {
    clearTokens();
    // 整页刷新而非只清 state：页面上的简历 / 投递列表本来就是按登录态拉的，
    // 刷新一次回到匿名视图最直观（与 lang-switch 的取舍一致）。
    window.location.reload();
  }

  if (!mounted) return null;

  return (
    <div className="auth-bar">
      {isLoggedIn() ? (
        <>
          <span className="meta">{email || "已登录"}</span>
          <button type="button" className="ghost" onClick={logout}>
            退出
          </button>
        </>
      ) : (
        <a className="link" href="/login">
          登录
        </a>
      )}
    </div>
  );
}