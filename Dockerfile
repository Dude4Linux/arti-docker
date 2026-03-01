# ── Stage 1: Build ────────────────────────────────────────────────────────────
# VERSION: a git tag (e.g. arti-v2.0.0), branch name (e.g. main), or "latest"
#          "latest" resolves to the most recent arti-v* release tag at build time.
ARG VERSION=latest

FROM rust:1.91.0-alpine3.22 AS builder

ARG VERSION

RUN apk add --no-cache \
    git \
    musl-dev \
    sqlite-dev \
    openssl-dev \
    perl \
    make \
    build-base

# Resolve "latest" to the most recent arti-v* tag; otherwise use VERSION as-is.
RUN if [ "$VERSION" = "latest" ]; then \
        git ls-remote --tags --sort=-v:refname \
            https://gitlab.torproject.org/tpo/core/arti.git 'refs/tags/arti-v*' \
        | head -1 | sed 's|.*refs/tags/||' > /ref; \
    else \
        echo "$VERSION" > /ref; \
    fi && \
    echo "Building ref: $(cat /ref)"

RUN git clone --depth 1 \
        --branch "$(cat /ref)" \
        https://gitlab.torproject.org/tpo/core/arti.git /build

WORKDIR /build
RUN cargo build -p arti --locked --release

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM alpine:3.22

RUN apk add --no-cache \
    sqlite-libs \
    ca-certificates

# Mirror the Debian packaging: dedicated _arti user, no shell, no home login
RUN addgroup -S _arti && \
    adduser -S -G _arti -H -s /sbin/nologin _arti

COPY --from=builder /build/target/release/arti /usr/local/bin/arti

# Config, cache, and state directories
RUN mkdir -p /etc/arti /var/lib/arti /var/cache/arti && \
    chown -R _arti:_arti /var/lib/arti /var/cache/arti

COPY docker/arti.toml /etc/arti/arti.toml

USER _arti
EXPOSE 9150

ENTRYPOINT ["/usr/local/bin/arti", "--config", "/etc/arti/arti.toml", "proxy"]
