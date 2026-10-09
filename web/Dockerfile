# Next.js standalone image: dependencies and the build in one stage, only the traced server and the
# static files in the one that runs. Not exposed to the host: Caddy is the single entry.

FROM node:24.15.0-alpine3.22@sha256:b689d4005875ae167178471a7a622ec2909459a3bbb32277260be1971af7a99f AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
# `npm ci` installs exactly what the lock file says and fails if it disagrees with package.json
RUN npm ci --no-audit --no-fund
COPY . .
# Only used if someone reaches this server directly: Caddy sends /api/* to the API itself.
ARG API_ORIGIN=http://api:8000
ENV API_ORIGIN=$API_ORIGIN
RUN npm run build

FROM node:24.15.0-alpine3.22@sha256:b689d4005875ae167178471a7a622ec2909459a3bbb32277260be1971af7a99f
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
# Unprivileged: nothing in here needs root at run time.
RUN adduser -S -u 10001 -H app
COPY --from=build --chown=10001 /app/.next/standalone ./
COPY --from=build --chown=10001 /app/.next/static ./.next/static
# Next writes its image/fetch cache here; the container's root file system is read-only and
# compose mounts a tmpfs on this path.
RUN mkdir -p .next/cache && chown 10001 .next/cache
USER 10001
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --retries=10 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
