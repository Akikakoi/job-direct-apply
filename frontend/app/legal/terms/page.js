// 用户协议（§14 ①）：静态服务端页面，语言跟随 cookie（与 /insights 同口径）。
// 正文（中英对照）在 ../legal-text.json——本节不再内联文案，避免"页面一份、送审稿一份"两处漂移。
import { LegalDoc, LegalSections, legalLang } from "../legal-doc";

export const metadata = {
  title: "用户协议 ｜ 简历直达",
  description: "简历直达用户协议：服务说明、授权范围、用户责任与协议变更",
};

export default async function TermsPage() {
  const lang = await legalLang();
  return (
    <LegalDoc lang={lang} docKey="terms">
      <LegalSections lang={lang} docKey="terms" />
    </LegalDoc>
  );
}