import "./globals.css";

export const metadata = {
  title: "简历直达",
  description: "基于企业官方 ATS 的职位推荐与直达投递",
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
