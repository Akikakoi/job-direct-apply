"use client";

import { useEffect, useState } from "react";

import { setTokens } from "../auth";

// 登录 / 注册页（§4.3）：注册成功直接返回令牌对，不需要再登录一次。
// `next` 从查询串读（不用 useSearchParams，省掉一层 Suspense 边界），只接受站内相对路径。

async function toJson(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export default function LoginPage() {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [next, setNext] = useState("/");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const target = new URLSearchParams(window.location.search).get("next") || "";
    // 防开放重定向：只认站内绝对路径（`//evil.com` 是协议相对 URL，必须挡掉）
    if (target.startsWith("/") && !target.startsWith("//")) setNext(target);
  }, []);

  async function submit(event) {
    event.preventDefault();
    if (loading) return;
    setError("");
    setLoading(true);
    try {
      const resp = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      const body = await toJson(resp);
      if (!resp.ok) {
        const label = mode === "login" ? "登录" : "注册";
        throw new Error(body?.detail || `${label}失败（服务端 ${resp.status}）`);
      }
      setTokens(body?.data);
      window.location.href = next;
    } catch (err) {
      setError(String(err.message || err));
      setLoading(false);
    }
  }

  return (
    <div className="container">
      <h1>简历直达</h1>
      <p className="sub">登录后简历与投递记录跟着账号走，换设备也不丢</p>

      <div className="card">
        <div className="row" style={{ marginBottom: 16 }}>
          <button
            type="button"
            className={mode === "login" ? undefined : "ghost"}
            onClick={() => {
              setMode("login");
              setError("");
            }}
          >
            登录
          </button>
          <button
            type="button"
            className={mode === "register" ? undefined : "ghost"}
            onClick={() => {
              setMode("register");
              setError("");
            }}
          >
            注册
          </button>
        </div>

        {error ? <div className="error" style={{ marginBottom: 12 }}>{error}</div> : null}

        <form className="auth-form" onSubmit={submit}>
          <label className="meta" htmlFor="email">邮箱</label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <label className="meta" htmlFor="password">口令</label>
          <input
            id="password"
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          {mode === "register" ? (
            <p className="meta" style={{ marginBottom: 10 }}>口令至少 8 位。</p>
          ) : null}
          <button type="submit" disabled={loading}>
            {loading ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>

        <p className="meta" style={{ marginTop: 14 }}>
          {mode === "register" ? "注册即表示你已阅读并同意" : "登录即表示你已阅读并同意"}{" "}
          <a className="link" href="/legal/terms">用户协议</a>
          {" ｜ "}
          <a className="link" href="/legal/privacy">隐私政策</a>
        </p>
      </div>

      <p className="meta">
        <a className="link" href="/">← 返回推荐首页</a>
      </p>
    </div>
  );
}