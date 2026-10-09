import type { NextConfig } from "next";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // The UI is a client-driven live terminal; all data comes from the API at runtime.
  cacheComponents: false,
  // Keep responses uncompressed so proxied Server-Sent Events are never buffered.
  compress: false,
  poweredByHeader: false,
  // The dev badge overlaps the mobile tab bar during demos.
  devIndicators: false,
  // The public HTTPS link (Cloudflare tunnel) may load dev resources; every other host stays blocked.
  allowedDevOrigins: ["countersign.everyrepo.me"],
  experimental: {
    // A full Attack Lab run takes about 30 s; the default 30 s rewrite timeout would turn it into a 500.
    proxyTimeout: 180_000,
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    // Same-origin proxy so the HttpOnly session cookie and CSRF cookie work without CORS.
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
