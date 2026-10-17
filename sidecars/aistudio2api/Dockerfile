# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Build context: this directory only; account files must never enter the context.
FROM golang:1.26-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS build
WORKDIR /src
RUN git init . && git remote add origin https://github.com/Mag1cFall/AIStudio2API.git \
    && git fetch --depth 1 origin 2cbe310ce6ebab093bf83e5078de8296f03770a9 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 2cbe310ce6ebab093bf83e5078de8296f03770a9
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY mixer-routing.patch /tmp/mixer-routing.patch
RUN git apply --check /tmp/mixer-routing.patch && git apply /tmp/mixer-routing.patch
# No Node/frontend build: only a static private-service information page is embedded.
COPY index.html internal/webui/dist/index.html
RUN --network=none --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go test ./internal/aistudio ./internal/api ./internal/app ./internal/config
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags='-s -w -buildid=' -o /out/aistudio2api ./cmd/aistudio2api
RUN mkdir -p /runtime-data/auth /runtime-data/runtime /licenses/upstream \
    && chown -R 10001:10001 /runtime-data \
    && cp LICENSE /licenses/upstream/LICENSE \
    && cp /tmp/mixer-routing.patch /licenses/upstream/mixer-routing.patch \
    && printf '%s\n' 'https://github.com/Mag1cFall/AIStudio2API' '2cbe310ce6ebab093bf83e5078de8296f03770a9' > /licenses/upstream/SOURCE \
    && find internal/waa -type f \( -name 'LICENSE*' -o -name 'COPYING*' -o -name 'NOTICE*' \) -exec cp --parents '{}' /licenses/upstream/ \;
RUN --mount=type=cache,target=/go/pkg/mod \
    find /go/pkg/mod -type f \( -iname 'license*' -o -iname 'copying*' -o -iname 'notice*' \) -exec cp --parents '{}' /licenses/ \;

FROM scratch AS runtime
LABEL org.opencontainers.image.source="https://github.com/Zis30axs/GeminiAPI-mixer" \
      org.opencontainers.image.description="Private AI Studio HTTP sidecar with strict source routing; no browser runtime" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.upstream.revision="2cbe310ce6ebab093bf83e5078de8296f03770a9"
COPY --from=build /out/aistudio2api /usr/local/bin/aistudio2api
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /usr/share/zoneinfo /usr/share/zoneinfo
COPY --from=build --chown=10001:10001 /runtime-data /data
COPY --from=build /licenses /usr/share/licenses
USER 10001:10001
WORKDIR /data
ENV HOME=/data LISTEN_ADDR=0.0.0.0:2048 WAA_BACKEND=go \
    WARM_WORKER_LIMIT=1 MAX_ACTIVE_WORKERS=1 WARM_STARTUP_CONCURRENCY=1 PER_ACCOUNT_CONCURRENCY=1 \
    UPSTREAM_CHANNELS=playground,build BUILD_NATIVE_NONSTREAM=false \
    AUTO_START=true REQUEST_BODY_LOG=false LOG_FILE=false GOMEMLIMIT=256MiB
EXPOSE 2048
ENTRYPOINT ["/usr/local/bin/aistudio2api"]
CMD ["-open-ui=false"]
