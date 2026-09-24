import { cookies } from "next/headers";

import "./globals.css";

import LangSwitch from "./lang-switch";
import ServiceWorkerRegister from "./sw-register";
import { LANG_COOKIE, normalizeLang } from "./i18n";

export const metadata = {
  title: "简历直达",
  description: "基于企业官方 ATS 的职位推荐与直达投递",
  applicationName: "简历直达",
  // iOS 添加到主屏后的表现（Safari 不读 manifest 的 display，只认这个 meta）
  appleWebApp: { capable: true, statusBarStyle: "default", title: "简历直达" },
};

// 移动端视口（§12.6 P5 ④）：Next.js 会把 viewport 渲染成 <meta name="viewport">，
// 并带 theme-color；缩放交给浏览器，不锁 user-scalable（无障碍要求）。
export const viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#2563eb",
};

export default async function RootLayout({ children }) {
  // 多语言看板（§12.6 P5 ⑤）：语言存 cookie，服务端渲染时应据此输出 <html lang> 与
  // 语言开关的高亮态（客户端只负责"切"）。读 cookie 会让路由转为按请求渲染——对本项目
  // 没有代价（页面数据全部来自 /api，本来就没有静态产物可复用）。
  const store = await cookies();
  const lang = normalizeLang(store.get(LANG_COOKIE)?.value);

  return (
    <html lang={lang}>
      <body>
        {children}
        <LangSwitch lang={lang} />
        <ServiceWorkerRegister />
      </body>
    </html>
  );
}