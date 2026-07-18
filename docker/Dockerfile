# PLANET --- PostgreSQL 18 with the planet extension built in.
#
# Build context is the repository root:
#     docker build -f docker/Dockerfile -t planet-pg .
#
# Calibration is deliberately a second-class citizen in here: fitting the
# coefficients needs RAPL, and RAPL is a host-wide counter that Docker masks by
# default. The normal path is to calibrate on the host and feed the fitted
# numbers in via config/coefficients.json (see initdb.d/20-planet-coefficients.sh).
# The scripts ship anyway so a host without psql/numpy can run the sweep inside
# the container, which needs `systempaths=unconfined` --- see compose.yaml.

ARG PG_IMAGE=postgres:18.4-trixie
ARG PG_MAJOR=18

# ---- build the extension -------------------------------------------------
FROM ${PG_IMAGE} AS build
ARG PG_MAJOR

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         build-essential \
         postgresql-server-dev-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/*

COPY extension/ /build/extension/
WORKDIR /build/extension

# with_llvm=no: PLANET is executor hooks and four multiplies. There is nothing
# for the JIT to inline, and skipping bitcode keeps clang/llvm out of the image.
RUN make with_llvm=no && make install with_llvm=no

# ---- runtime -------------------------------------------------------------
FROM ${PG_IMAGE}
ARG PG_MAJOR

LABEL org.opencontainers.image.title="planet-pg" \
      org.opencontainers.image.description="PostgreSQL 18 + PLANET per-query carbon accounting" \
      org.opencontainers.image.source="https://github.com/green-coding-solutions/planet" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

# python3 for apply-coefficients.py (stdlib only); scipy/numpy so the
# calibration scripts can also run in here if the host lacks them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 \
         python3-scipy \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/lib/postgresql/${PG_MAJOR}/lib/planet.so \
                  /usr/lib/postgresql/${PG_MAJOR}/lib/
COPY --from=build /usr/share/postgresql/${PG_MAJOR}/extension/planet.control \
                  /usr/share/postgresql/${PG_MAJOR}/extension/planet--*.sql \
                  /usr/share/postgresql/${PG_MAJOR}/extension/

COPY calibration/ /opt/planet/calibration/
COPY config/      /opt/planet/config/

COPY docker/planet.conf           /etc/postgresql/planet.conf
COPY docker/apply-coefficients.py /usr/local/bin/planet-apply-coefficients
COPY docker/initdb.d/             /docker-entrypoint-initdb.d/

# The initdb.d scripts MUST be executable. docker-entrypoint.sh `source`s any
# .sh it cannot execute, which would run them inside the entrypoint's own shell:
# their `set -u` would leak into it, and any `exec` would replace the postmaster
# before it ever starts (the container then exits 0 right after initdb).
RUN chmod +x /usr/local/bin/planet-apply-coefficients \
             /docker-entrypoint-initdb.d/*.sh
