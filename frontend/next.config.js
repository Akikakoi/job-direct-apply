/** @type {import('next').NextConfig} */
// 开发期把 /api 代理到 FastAPI 后端（backend/ 默认 8001 端口；
// 8000 让给 stellar-mall 后端，避免两个项目互相挤占）
const nextConfig = {
  // 关掉开发态左下角的 Next.js DevTools 浮标：本项目自己的固定浮层只在顶部
  // （右上语言开关 / 左上账号状态条），左下那个圆标容易被误认成产品入口。
  devIndicators: false,
  // dev/生产代理超时（默认 30s）。简历解析要调 LLM（MiMo），常超 30s，
  // 超时会被 Next.js 代理掐断 → ECONNRESET → 前端报 500，但后端其实已入库。
  // 2026-10-20 实测：重传简历 LLM 解析 + 重试耗时 185s > 180s 仍旧踩线 → 500，
  // 放宽到 10 分钟（后端自身有 5MB/类型校验与超时兜底，代理只是别插手）。
  experimental: {
    proxyTimeout: 600000,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8001/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;
