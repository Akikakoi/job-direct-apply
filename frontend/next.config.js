/** @type {import('next').NextConfig} */
// 开发期把 /api 代理到 FastAPI 后端（backend/ 默认 8001 端口；
// 8000 让给 stellar-mall 后端，避免两个项目互相挤占）
const nextConfig = {
  // dev 代理超时（默认 30s）。简历解析要调 LLM（MiMo），常超 30s，
  // 超时会被 Next.js 代理掐断 → ECONNRESET → 前端报 500，但后端其实已入库。
  experimental: {
    proxyTimeout: 180000,
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
