const path = require("path");
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  experimental: { outputFileTracingRoot: path.join(__dirname, "../..") },
  async rewrites() {
    const registryUrl = process.env.REGISTRY_URL || process.env.NEXT_PUBLIC_REGISTRY_URL || "http://localhost:3030";
    const gatewayUrl = process.env.GATEWAY_URL || process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:4030";
    const bapUrl = process.env.BAP_URL || process.env.NEXT_PUBLIC_BAP_URL || "http://localhost:8002";
    const bppUrl = process.env.BPP_URL || process.env.NEXT_PUBLIC_BPP_URL || "http://localhost:8001";
    return [
      {
        source: "/api/registry/:path*",
        destination: `${registryUrl}/:path*`,
      },
      {
        source: "/api/gateway/:path*",
        destination: `${gatewayUrl}/:path*`,
      },
      {
        source: "/api/bap/:path*",
        destination: `${bapUrl}/:path*`,
      },
      {
        source: "/api/bpp/:path*",
        destination: `${bppUrl}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
