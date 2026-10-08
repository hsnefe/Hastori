import type { NextConfig } from "next";

// In development the browser talks to this server only (127.0.0.1:3000), so the refresh cookie
// (SameSite=Strict, Path=/api/v1/auth) is first-party. Packaged, Caddy does the same job.
const apiOrigin = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default config;
