// PWA 清单（§12.6 P5 ④）：Next.js App Router 的 manifest 元数据路由，
// 构建后由 Next 自动在 <head> 注入 <link rel="manifest" href="/manifest.webmanifest">。
// 图标当前只有 SVG（sizes: "any"）——位图 192/512 安装图标属发布前补齐（见开发文档 §12.6）。
export default function manifest() {
  return {
    name: "简历直达 · Job Direct Apply",
    short_name: "简历直达",
    description: "基于企业官方 ATS 的职位推荐与直达投递",
    lang: "zh-CN",
    start_url: "/",
    scope: "/",
    display: "standalone",
    orientation: "portrait",
    background_color: "#f6f7f9",
    theme_color: "#2563eb",
    icons: [
      { src: "/icons/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any" },
      { src: "/icons/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "maskable" },
    ],
  };
}