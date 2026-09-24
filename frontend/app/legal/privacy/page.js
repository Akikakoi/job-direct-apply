// 隐私政策（§14 ①）：必须覆盖括号内三项要求——个人信息授权、删除、最小必要。
// 正文（中英对照）在 ../legal-text.json；强制披露的小节 id 由后端 legal.REQUIRED_SECTION_IDS
// 声明、backend/tests/test_legal.py 断言中英两套都在，删除口径与
// DELETE /api/resumes/{id}（连带删磁盘原件）保持一致。
import { LegalDoc, LegalSections, legalLang } from "../legal-doc";

export const metadata = {
  title: "隐私政策 ｜ 简历直达",
  description: "简历直达隐私政策：最小必要收集、授权范围、个人信息删除与存储安全",
};

export default async function PrivacyPage() {
  const lang = await legalLang();
  return (
    <LegalDoc lang={lang} docKey="privacy">
      <LegalSections lang={lang} docKey="privacy" />
    </LegalDoc>
  );
}