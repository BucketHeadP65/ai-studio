# syntax=docker/dockerfile:1
#
# The local Postgres: a self-contained PostgreSQL 17 image for the engram `pg_textsearch`
# dialect, so the full pipeline (migrate, index, serve, AGE multi-hop) can be developed
# and tested offline. See engram ADR-0006 (dual-dialect BM25 seam) and ADR-0007 (AGE multi-hop).
#
# It bundles four extensions, each installable via `CREATE EXTENSION` at runtime:
#   - pg_textsearch  (OSS BM25; the `<@>` / to_bm25query grammar)           — built from source
#   - age            (Apache AGE; property graph + openCypher)             — built from source
#   - vector         (pgvector; halfvec / HNSW / `<=>`)                      — built from source
#   - ltree          (hierarchical section paths)                          — ships in postgresql-contrib
#
# The CREATE EXTENSION statements themselves live in the SIBLING file
# `initdb/10-extensions.sql`; the compose service (docker-compose.yml,
# `postgres`) mounts it into the entrypoint so it runs once at first
# boot. This image only makes the extensions *available* and preloads the two
# libraries that demand it (see shared_preload_libraries below).
#
# Built and run by the compose `postgres` service: build context this dir,
# published 5434:5432, env POSTGRES_DB=knowledge_bases POSTGRES_USER=wiki POSTGRES_PASSWORD=wiki.

# Pin the Debian variant for reproducible source builds (PGDG apt sources baked in).
FROM postgres:18-bookworm

# --- Pinned extension versions (stable, the extensions the managed Postgres target ships, see retrieval-concepts.md) ---
#   AGE: the PG17 branch advanced to 1.6.0; PG17/v1.6.0-rc0 is the canonical 1.6.0
#        source tag for PostgreSQL 17 (Apache AGE supports PG 11–18).
#   pg_textsearch: v1.3.0 is the current stable release.
#   pgvector: v0.8.2 (current stable patch of the 0.8.x line, which
#        ships halfvec, HNSW and the <=> operators engram relies on).
ARG AGE_REF=PG17/v1.6.0-rc0
ARG PG_TEXTSEARCH_REF=v1.3.0
ARG PGVECTOR_REF=v0.8.2

# --- Build all three from-source extensions in ONE layer, then purge the toolchain ---
# Compiled artifacts (.so → $libdir, .control/.sql → extension share dir) survive the
# dev-package removal, so the final image stays lean while CREATE EXTENSION still works.
# `make` for pg_textsearch / pgvector; AGE additionally needs flex/bison + readline/zlib.
RUN set -eux; \
    savedAptMark="$(apt-mark showmanual)"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        flex \
        bison \
        libreadline-dev \
        zlib1g-dev \
        "postgresql-server-dev-$PG_MAJOR"; \
    \
    # ltree needs NO install step: the official postgres:17 image already bundles all
    # postgresql-contrib modules, so `CREATE EXTENSION ltree` works out of the box.
    \
    mkdir -p /tmp/build; \
    \
    # --- pgvector (vector / halfvec / HNSW / <=>) ---
    git clone --branch "$PGVECTOR_REF" --depth 1 https://github.com/pgvector/pgvector.git /tmp/build/pgvector; \
    cd /tmp/build/pgvector; \
    make clean; \
    make OPTFLAGS=""; \
    make install; \
    \
    # --- pg_textsearch (OSS BM25, the <@> / to_bm25query grammar) ---
    git clone --branch "$PG_TEXTSEARCH_REF" --depth 1 https://github.com/timescale/pg_textsearch.git /tmp/build/pg_textsearch; \
    cd /tmp/build/pg_textsearch; \
    make; \
    make install; \
    \
    # --- Apache AGE (property graph + openCypher) ---
    git clone --branch "$AGE_REF" --depth 1 https://github.com/apache/age.git /tmp/build/age; \
    cd /tmp/build/age; \
    make; \
    make install; \
    \
    # --- Purge the build toolchain to shrink the image; keep runtime libs ---
    cd /; \
    rm -rf /tmp/build; \
    apt-mark auto '.*' > /dev/null; \
    apt-mark manual $savedAptMark > /dev/null; \
    apt-get purge -y --auto-remove \
        -o APT::AutoRemove::RecommendsImportant=false \
        build-essential git flex bison libreadline-dev zlib1g-dev "postgresql-server-dev-$PG_MAJOR"; \
    rm -rf /var/lib/apt/lists/*

# --- Preload the libraries that MUST be in shared_preload_libraries ---
# AGE will not load without 'age'; pg_textsearch likewise requires 'pg_textsearch'
# in shared_preload_libraries (both projects' install docs state this verbatim).
# We append to the image's postgresql.conf.sample so the setting lands in the data
# dir initdb generates at first boot — no custom compose `command:` override needed.
# (pgvector and ltree do NOT need preloading; CREATE EXTENSION alone suffices.)
RUN set -eux; \
    echo "" >> /usr/share/postgresql/postgresql.conf.sample; \
    echo "# engram postgres: extensions that require library preload (AGE, pg_textsearch)." >> /usr/share/postgresql/postgresql.conf.sample; \
    echo "shared_preload_libraries = 'age,pg_textsearch'" >> /usr/share/postgresql/postgresql.conf.sample

# CREATE EXTENSION lines run at first boot from the SIBLING initdb/10-extensions.sql,
# mounted into /docker-entrypoint-initdb.d/ by the compose `postgres` service
# (NOT copied here, to keep the SQL editable without an image rebuild). The base
# image's entrypoint, ports (5432), and POSTGRES_* env handling are inherited as-is.
