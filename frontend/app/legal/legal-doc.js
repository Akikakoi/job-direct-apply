// 法务文档外壳（§14 ①）：政策正文放前端静态资产，后端只留版本与告知要点。
//
// 正文单一事实源 = `legal-text.json`（中英逐条对照，改正文只改这一个文件）；
// 本文件只负责渲染与"版本号"——版本号/生效日必须与后端 app/services/legal.py 的
// POLICY_VERSION / POLICY_EFFECTIVE_DATE 一致：它们不只是一行文案，而是
// 「用户同意了哪一版」的可核验凭据（applications.consent_version 指向该版本）。
// 跨端漂移会让同意记录失效，故由 backend/tests/test_legal.py 做契约断言，而不是靠人工同步。
import { cookies } from "next/headers";

import { LANG_COOKIE, normalizeLang, t } from "../i18n";
import LEGAL_TEXT from "./legal-text.json";

export const POLICY_VERSION = "1.0";
export const POLICY_EFFECTIVE_DATE = "2026-09-24";

export async function legalLang() {
  const store = await cookies();
  return normalizeLang(store.get(LANG_COOKIE)?.value);
}

// 正文取词：cookie 里的语言码是 zh-CN/en（与 i18n 词典同口径），正文 JSON 用 zh/en 两套；
// 这里做一次映射——**认不出的语言一律回中文**（宁可显示中文也不能给空白页）。
export function textLang(lang) {
  return normalizeLang(lang) === "en" ? "en" : "zh";
}

export function legalSections(docKey, lang) {
  const doc = LEGAL_TEXT[docKey] ?? {};
  return doc[textLang(lang)] ?? doc.zh ?? [];
}

export function legalDisclaimer(lang) {
  const text = LEGAL_TEXT.disclaimer ?? {};
  return text[textLang(lang)] ?? text.zh ?? "";
}

// 行内强调：正文以"纯文本 + **粗体**"存放，页面对字符串直接切分渲染。
// 不引 markdown 解析器——正文里只有粗体一种标记，切一刀即可，
// 更重要的是送审稿（scripts/export_legal_text.py）与页面渲染的是同一份字符串，不会各改各的。
function Inline({ text }) {
  return String(text)
    .split(/\*\*(.+?)\*\*/g)
    .map((part, i) => (i % 2 ? <strong key={i}>{part}</strong> : part));
}

export function LegalSections({ lang, docKey }) {
  return legalSections(docKey, lang).map((section) => (
    <section key={section.id} id={section.id}>
      <h2>{section.h}</h2>
      {section.body.map((block, i) =>
        block.ul ? (
          <ul key={i}>
            {block.ul.map((item, j) => (
              <li key={j}>
                <Inline text={item} />
              </li>
            ))}
          </ul>
        ) : (
          <p key={i}>
            <Inline text={block.p} />
          </p>
        )
      )}
    </section>
  ));
}

// 段落级排版复用页面既有样式（.card / .sub / .meta / .link），不新造 CSS 类
export function LegalDoc({ lang, docKey, children }) {
  const tt = (key, vars) => t(lang, key, vars);
  const other = docKey === "terms" ? "privacy" : "terms";
  return (
    <div className="container">
      <h1>{tt(`legal_${docKey}_title`)}</h1>
      <p className="meta">
        {tt("legal_version", { v: POLICY_VERSION, d: POLICY_EFFECTIVE_DATE })}
      </p>
      <div className="card">{children}</div>
      <p className="meta" style={{ marginTop: 16 }}>
        {legalDisclaimer(lang)}
      </p>
      <p style={{ marginBottom: 20 }}>
        <a className="link" href="/">
          {tt("legal_back_home")}
        </a>
        {" ｜ "}
        <a className="link" href={`/legal/${other}`}>
          {tt(`legal_link_${other}`)}
        </a>
      </p>
    </div>
  );
}