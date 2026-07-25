# Postgres MIT der Original-Extension pg_acoustid — ausschliesslich fuer Tests.
#
# Wozu: die Matching-Pipeline rechnet den AcoustID-Score in Python nach
# (`shared.fingerprint.compare2`, `shared.fpindex.extract_query`). Damit dieser
# Nachbau nicht stillschweigend abdriftet, vergleicht ein Test ihn bit-genau
# gegen die autoritative C-Implementierung. Dieses Image ist die Referenz.
#
# WICHTIG — dieses Image geht NIE in den Betrieb (DECISIONS 2026-07-25
# "Rescoring per Python-Nachbau mit CI-Bit-Verifikation"): pg_acoustid hat
# keine Lizenzdatei und darf deshalb nicht weiterverbreitet werden. Der
# Quelltext wird hier zur Bauzeit geholt und liegt bewusst NICHT im Repo; das
# fertige Image bleibt lokal bzw. im CI-Runner und wird nirgends gepusht.
# Der Stack selbst faehrt das unveraenderte offizielle `postgres:18`.
#
# Bauen und starten (siehe auch shared/tests/test_fingerprint_extension.py):
#
#   docker build -t acoustid-offline-pg-acoustid:test tests/pg_acoustid
#   docker run -d --rm --name pg-acoustid -p 127.0.0.1:5443:5432 \
#     -e POSTGRES_PASSWORD=test acoustid-offline-pg-acoustid:test
#   ACOUSTID_EXTENSION_DSN=postgresql://postgres:test@127.0.0.1:5443/postgres \
#     uv run pytest -m extension --integration=require
#
# Gleiche Major-Version wie der Stack (docker-compose.yml), damit die
# Extension gegen dieselbe Server-API gebaut wird.
FROM postgres:18

# Commit statt Branch: das Repo hat keine Releases und wird kaum gepflegt —
# ein bewegliches `main` wuerde die Referenz unter dem Test wegziehen.
# Stand 2021-12-27 (letzter Commit des Projekts).
ARG PG_ACOUSTID_COMMIT=102f3c870c6157704694c2ddbad3ae8ab2c7de91

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        "postgresql-server-dev-${PG_MAJOR}"; \
    curl -fsSL "https://codeload.github.com/acoustid/pg_acoustid/tar.gz/${PG_ACOUSTID_COMMIT}" \
        | tar -xz -C /tmp; \
    make -C "/tmp/pg_acoustid-${PG_ACOUSTID_COMMIT}"; \
    make -C "/tmp/pg_acoustid-${PG_ACOUSTID_COMMIT}" install; \
    rm -rf "/tmp/pg_acoustid-${PG_ACOUSTID_COMMIT}"; \
    apt-get purge -y --auto-remove build-essential curl "postgresql-server-dev-${PG_MAJOR}"; \
    rm -rf /var/lib/apt/lists/*

# Das offizielle Image fuehrt alles unter /docker-entrypoint-initdb.d beim
# Erststart aus — danach steht `acoustid_compare2` und `acoustid_extract_query`
# in der Standarddatenbank bereit.
COPY create-extension.sql /docker-entrypoint-initdb.d/10-create-extension.sql
