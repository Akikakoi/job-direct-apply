"use client";

// 账号令牌 + 带鉴权的 fetch（§4.3 JWT 鉴权的前端落点）
//
// 令牌存 localStorage 而非 cookie：后端只认 `Authorization: Bearer`（无 cookie 会话），
// 用 cookie 反而要自己处理"谁读谁写"和 CSRF。代价是 XSS 可读——本项目不引第三方脚本、
// 服务端也不渲染用户私有数据，先按这个折中走；真要收紧再改 httpOnly cookie + 后端读 cookie。
//
// 本模块只在客户端有实际行为（读 localStorage / 跳转），所有函数对 SSR 做空值兜底。

export const ACCESS_KEY = "access_token";
export const REFRESH_KEY = "refresh_token";

const LOGIN_PATH = "/login";

function storage() {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    // 隐私模式等场景 localStorage 可能直接抛错：按"未登录"处理，不炸页面
    return null;
  }
}

export function getAccessToken() {
  return storage()?.getItem(ACCESS_KEY) || "";
}

export function getRefreshToken() {
  return storage()?.getItem(REFRESH_KEY) || "";
}

/** 登录 / 注册 / 刷新接口的 data 里直接带令牌对，原样落盘即可。 */
export function setTokens(data) {
  const store = storage();
  if (!store || !data) return;
  if (data.access_token) store.setItem(ACCESS_KEY, data.access_token);
  if (data.refresh_token) store.setItem(REFRESH_KEY, data.refresh_token);
}

export function clearTokens() {
  const store = storage();
  if (!store) return;
  store.removeItem(ACCESS_KEY);
  store.removeItem(REFRESH_KEY);
}

/** 有任一枚令牌即视为"客户端自认已登录"（access 过期后靠 refresh 自愈）。 */
export function isLoggedIn() {
  return Boolean(getAccessToken() || getRefreshToken());
}

// 并发 401 只换一次令牌：页面首屏往往同时打多个接口，过期时不该打出一串 refresh
let refreshing = null;

function refreshTokens() {
  if (refreshing) return refreshing;
  const refresh = getRefreshToken();
  if (!refresh) return Promise.resolve(false);
  refreshing = (async () => {
    try {
      const resp = await fetch("/api/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!resp.ok) return false;
      const data = (await resp.json())?.data;
      if (!data?.access_token) return false;
      setTokens(data);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

function toLogin() {
  clearTokens();
  if (window.location.pathname.startsWith(LOGIN_PATH)) return; // 防自跳
  const next = window.location.pathname + window.location.search;
  window.location.href = `${LOGIN_PATH}?next=${encodeURIComponent(next)}`;
}

async function rawFetch(url, init, token) {
  const headers = new Headers(init.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(url, { ...init, headers });
}

/**
 * 带鉴权的 fetch：自动附 `Authorization: Bearer`；遇 401 先用 refresh 令牌换一次令牌再重试一次。
 *
 * 换不到就清空令牌并跳登录页（带 next 回跳）——"客户端自认登录、后端却拒绝"只剩这一个可自愈动作。
 * `redirectOnFail: false` 给**不该触发跳转**的调用用：如 `/api/auth/me` 这种登录态自检，
 * 未登录返回 401 是正常答案，不是令牌过期。
 */
export async function authFetch(url, options = {}) {
  const { redirectOnFail = true, ...init } = options;
  const resp = await rawFetch(url, init, getAccessToken());
  if (resp.status !== 401) return resp;
  if (await refreshTokens()) {
    return rawFetch(url, init, getAccessToken());
  }
  if (redirectOnFail) {
    toLogin(); // 内部已 clearTokens
  } else {
    clearTokens();
  }
  return resp;
}