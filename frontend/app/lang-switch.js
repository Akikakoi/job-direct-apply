"use client";

import { LANGS, langCookieValue, normalizeLang, t } from "./i18n";

// 语言开关（§12.6 P5 ⑤）：写 cookie 后整页刷新。
// 为什么刷新而不是只改 state：<html lang> 与全部文案都由服务端按 cookie 渲染
// （见 app/layout.js），只切客户端 state 会出现"文案英文、lang 属性仍是中文"的错配。
export default function LangSwitch({ lang }) {
  const current = normalizeLang(lang);

  function switchTo(code) {
    if (code === current) return;
    document.cookie = langCookieValue(code);
    window.location.reload();
  }

  return (
    <div className="lang-switch" aria-label={t(current, "lang_label")}>
      {LANGS.map((item) => (
        <button
          key={item.code}
          type="button"
          className={item.code === current ? "ghost lang-active" : "ghost"}
          aria-pressed={item.code === current}
          onClick={() => switchTo(item.code)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}