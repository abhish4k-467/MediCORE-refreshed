import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  outputFileTracingRoot: __dirname,
  allowedDevOrigins: [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://192.168.29.44:3000",
    "http://192.168.29.215:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
  ],
};

export default nextConfig;
