/** @type {import('next').NextConfig} */
// 开发期把 /api 代理到 FastAPI 后端（backend/ 默认 8001 端口；
// 8000 让给 stellar-mall 后端，避免两个项目互相挤占）
const nextConfig = {
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
