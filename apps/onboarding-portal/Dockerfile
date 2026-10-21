# Node 24 / pnpm 9.12; repository-root context required.
FROM node:24-bookworm-slim AS builder
ENV NEXT_TELEMETRY_DISABLED=1
RUN corepack enable && corepack prepare pnpm@9.12.0 --activate
WORKDIR /repo
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/onboarding-portal/package.json apps/onboarding-portal/package.json
COPY packages/beckn-protocol-js/package.json packages/beckn-protocol-js/package.json
COPY packages/channel-adapter/package.json packages/channel-adapter/package.json
COPY packages/network-extension/package.json packages/network-extension/package.json
RUN pnpm install --frozen-lockfile
COPY apps/onboarding-portal apps/onboarding-portal
# These public values and rewrites are embedded by next build. Rebuild to change.
ARG NEXT_PUBLIC_FIREBASE_API_KEY
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID
ARG NEXT_PUBLIC_FIREBASE_STORAGE_BUCKET
ARG NEXT_PUBLIC_FIREBASE_MESSAGING_SENDER_ID
ARG NEXT_PUBLIC_FIREBASE_APP_ID
ARG REGISTRY_URL=http://registry:3030
ARG GATEWAY_URL=http://gateway:4030
ARG BAP_URL=http://bap:8003
ARG BPP_URL=http://external-bpp.invalid:8001
RUN mkdir -p apps/onboarding-portal/public && pnpm --filter @jaringan-dagang/onboarding-portal build

FROM node:24-bookworm-slim AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3001 HOSTNAME=0.0.0.0
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
COPY --from=builder --chown=app:app /repo/apps/onboarding-portal/.next/standalone ./
COPY --from=builder --chown=app:app /repo/apps/onboarding-portal/.next/static ./apps/onboarding-portal/.next/static
COPY --from=builder --chown=app:app /repo/apps/onboarding-portal/public ./apps/onboarding-portal/public
USER 10001:10001
EXPOSE 3001
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 CMD ["node", "-e", "fetch('http://127.0.0.1:3001').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]
CMD ["node", "apps/onboarding-portal/server.js"]
