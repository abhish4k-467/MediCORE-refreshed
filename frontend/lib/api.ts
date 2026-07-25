const PRODUCTION_API_URL = "https://medicore-production-0aac.up.railway.app";
const CHAT_WS_PATH = "/ws/chat";

function isLocalHostname(hostname: string) {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "0.0.0.0" ||
    hostname.startsWith("192.168.") ||
    hostname.startsWith("10.") ||
    hostname.startsWith("172.") ||
    hostname.endsWith(".local")
  );
}

function normalizeBrowserUrl(url: string) {
  if (typeof window === "undefined") {
    return url;
  }

  if (window.location.protocol !== "https:" || !url.startsWith("http://")) {
    return url;
  }

  try {
    const parsed = new URL(url);
    if (!isLocalHostname(parsed.hostname)) {
      parsed.protocol = "https:";
      return parsed.toString().replace(/\/$/, "");
    }
  } catch {
    return url;
  }

  return url;
}

export function getApiBaseUrl() {
  if (process.env.NEXT_PUBLIC_API_URL) {
    return normalizeBrowserUrl(process.env.NEXT_PUBLIC_API_URL).replace(/\/$/, "");
  }

  if (typeof window === "undefined") {
    return PRODUCTION_API_URL;
  }

  const hostname = window.location.hostname;
  if (isLocalHostname(hostname)) {
    const targetHost = hostname === "localhost" ? "127.0.0.1" : hostname;
    return `${window.location.protocol}//${targetHost}:8000`;
  }

  return PRODUCTION_API_URL;
}

export function getChatWsUrl() {
  if (process.env.NEXT_PUBLIC_WS_URL) {
    const wsUrl = process.env.NEXT_PUBLIC_WS_URL;
    if (typeof window !== "undefined" && window.location.protocol === "https:" && wsUrl.startsWith("ws://")) {
      return `wss://${wsUrl.slice("ws://".length)}`;
    }
    return wsUrl;
  }

  if (typeof window === "undefined") {
    return PRODUCTION_API_URL.replace(/^http/, "ws") + CHAT_WS_PATH;
  }

  const hostname = window.location.hostname;
  if (isLocalHostname(hostname)) {
    const targetHost = hostname === "localhost" ? "127.0.0.1" : hostname;
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    return `${protocol}://${targetHost}:8000${CHAT_WS_PATH}`;
  }

  return PRODUCTION_API_URL.replace(/^http/, "ws") + CHAT_WS_PATH;
}
