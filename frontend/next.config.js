/** @type {import('next').NextConfig} */
// 开发期把 /api 代理到 FastAPI 后端（backend/ 默认 8000 端口）
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8000/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;
