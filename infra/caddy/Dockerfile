# Stock Caddy has no rate limiter: build it with mholt/caddy-ratelimit (pinned to a commit; the
# last tag, v0.1.0, predates the Caddy releases it now builds against).
FROM caddy:2.11.4-builder-alpine@sha256:0aa610043dab5da82ad0a0268e46bb852785e6f5160f12f1c6fe3f42903d7e1b AS build
# xcaddy copies the file capability cap_net_bind_service of the stock binary onto its result;
# `cat` into a new file leaves it behind.
RUN xcaddy build --output /tmp/caddy-cap --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db     && cat /tmp/caddy-cap > /tmp/caddy && chmod 755 /tmp/caddy

FROM caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b
# The stock binary carries the file capability cap_net_bind_service, and COPY onto an existing
# file keeps it: remove the file first. This binary listens on 8080 only and needs no capability
# (compose drops them all), so the build fails if one is left.
RUN rm /usr/bin/caddy
COPY --from=build /tmp/caddy /usr/bin/caddy
RUN caddy list-modules | grep -q '^http.handlers.rate_limit$'     && test -z "$(getcap /usr/bin/caddy)"
