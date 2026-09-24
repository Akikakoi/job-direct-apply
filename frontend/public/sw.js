// 简历直达 Service Worker（§12.6 P5 ④）
//
// 策略（离线可用的最小充分集，不做过度缓存）：
// - 安装时预缓存应用外壳（首页 / 质量看板 / 图标 / manifest）；
// - 导航请求 network-first：在线取最新页面，离线回退缓存的外壳；
// - 同源静态资源（/_next/static、外壳）stale-while-revalidate：先用缓存秒开，后台刷新；
// - **/api/ 请求一律不缓存**：职位、推荐、简历都是强时效 + 隐私数据，缓存会造成
//   陈旧结果与跨用户泄露风险——离线时这些请求直接失败由页面自行提示。
const CACHE = "jda-shell-v1";
const SHELL = ["/", "/insights", "/manifest.webmanifest", "/icons/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // 时效 + 隐私：绝不缓存 API

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/").then((r) => r || Response.error()))
    );
    return;
  }

  if (url.pathname.startsWith("/_next/static") || SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const network = fetch(req)
          .then((resp) => {
            caches.open(CACHE).then((cache) => cache.put(req, resp.clone()));
            return resp;
          })
          .catch(() => cached);
        return cached || network;
      })
    );
  }
});