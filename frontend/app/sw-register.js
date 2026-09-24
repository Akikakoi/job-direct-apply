"use client";

// Service Worker 注册（§12.6 P5 ④）：浏览器支持才注册，失败静默——PWA 是增强项，
// 注册失败不能让页面报错（隐私模式/旧浏览器/HTTP 非 localhost 都会拒绝）。
import { useEffect } from "react";

export default function ServiceWorkerRegister() {
  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    const register = () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    };
    if (document.readyState === "complete") {
      register();
      return;
    }
    window.addEventListener("load", register);
    return () => window.removeEventListener("load", register);
  }, []);

  return null;
}