# Ein Image, ein Container (HANDOFF v2 §3, DECISIONS 2026-08-04).
#
#   docker compose build
#
# Enthaelt alles, was die Instanz braucht: die Python-Anwendung (Waechter,
# API, Importer), PostgreSQL 18 und den aus der Quelle gebauten
# acoustid-index. Gesteuert werden die Dauerdienste von supervisord unter
# `tini` als PID 1 (E1); Jobs sind Subprozesse des Waechters (E10).
#
# **Nur linux/amd64** (E3): der acoustid-index ist bislang nur dort erprobt;
# der aarch64-Spike steht vor M9 an. Auf Apple Silicon mit colima
# `--vz-rosetta` laeuft das Image.
#
# Vier Stufen:
#   fpindex-build   Zig-Toolchain, baut das Index-Binary aus der Quelle
#   app-build       uv, baut das venv aus uv.lock (alle drei Pakete)
#   supervisor      supervisord in ein eigenes venv (nicht ins App-venv)
#   runtime         Debian + Postgres 18 + die drei Ergebnisse

# --- fpindex aus der Quelle (GPL-3.0, E7) -----------------------------------
#
# Commit-Pin statt Digest-Pin: aus dem gepinnten Image von v1 wird der Stand,
# aus dem gebaut wird. `ACOUSTID_INDEX_COMMIT` ist derselbe Stand, den
# docker-compose.yml (v1) per Digest festhielt (main, 2025-10-27).
# Quelle, Lizenz und Commit stehen in THIRD-PARTY-NOTICES.md und in den
# OCI-Labels des Images (Quellangebot).
FROM debian:bookworm-slim AS fpindex-build

ARG ACOUSTID_INDEX_REPO=https://github.com/acoustid/acoustid-index.git
ARG ACOUSTID_INDEX_COMMIT=6bc929a316e4f3a9c9ec37a395f30e0f5b7116c2
# `minimum_zig_version` aus build.zig.zon des gepinnten Stands.
ARG ZIG_VERSION=0.14.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://ziglang.org/download/${ZIG_VERSION}/zig-linux-x86_64-${ZIG_VERSION}.tar.xz" \
        -o /tmp/zig.tar.xz \
    && mkdir -p /opt/zig \
    && tar -xJf /tmp/zig.tar.xz -C /opt/zig --strip-components=1 \
    && rm /tmp/zig.tar.xz \
    && ln -s /opt/zig/zig /usr/local/bin/zig \
    && zig version

WORKDIR /src
RUN git init --quiet . \
    && git remote add origin "${ACOUSTID_INDEX_REPO}" \
    && git fetch --quiet --depth 1 origin "${ACOUSTID_INDEX_COMMIT}" \
    && git checkout --quiet FETCH_HEAD \
    && git rev-parse HEAD > /src/COMMIT

# `--release=fast` ist die Einstellung des Upstream-Builds
# (.github/workflows/build.yml); nur so entsteht dasselbe Artefakt.
RUN zig build --release=fast --summary all \
    && /src/zig-out/bin/fpindex --help >/dev/null 2>&1 || true
RUN test -x /src/zig-out/bin/fpindex

# Das Quellangebot der GPL-3.0 wird mit ausgeliefert: ein Tarball des
# gebauten Stands liegt im Image (E7).
RUN git archive --format=tar.gz --prefix="acoustid-index-${ACOUSTID_INDEX_COMMIT}/" \
        -o /src/acoustid-index-source.tar.gz HEAD

# --- Python-Anwendung -------------------------------------------------------
#
# Basis ist das offizielle uv-Image auf python:3.14-slim-bookworm — dieselbe
# Werkzeugkette wie in CI und lokal, damit im Container genau die Versionen
# aus uv.lock landen (Muster der v1-Dockerfiles).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS app-build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Erst die Metadaten aller Workspace-Member (uv braucht sie, um den Workspace
# aufzuloesen), dann die Abhaengigkeiten — die Schicht bleibt im Cache,
# solange sich uv.lock nicht aendert.
COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY api/pyproject.toml api/
COPY importer/pyproject.toml importer/
COPY watchdog/pyproject.toml watchdog/
RUN uv sync --frozen --no-dev --no-install-workspace --all-packages

# Jetzt der eigene Code. Anders als in v1 sind es **alle** Pakete: ein Image
# traegt Waechter, API und Importer.
COPY shared/ shared/
COPY api/ api/
COPY importer/ importer/
COPY watchdog/ watchdog/
RUN uv sync --frozen --no-dev --all-packages

# --- supervisord ------------------------------------------------------------
#
# Bewusst ein **eigenes** venv: supervisord ist Werkzeug des Images, keine
# Abhaengigkeit der Anwendung — im App-venv wuerde es uv.lock aufblaehen und
# bei jedem `uv sync` mitwandern. Verifiziert unter Python 3.14 (M1b-Pruefpunkt
# E1: PyPI weist bis 3.13 aus, die Version laeuft aber).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS supervisor-build

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

RUN uv venv /opt/supervisor \
    && VIRTUAL_ENV=/opt/supervisor uv pip install --no-cache "supervisor==4.3.0" \
    && /opt/supervisor/bin/supervisord --version

# --- Laufzeit ---------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

ARG ACOUSTID_INDEX_COMMIT=6bc929a316e4f3a9c9ec37a395f30e0f5b7116c2
ARG PG_MAJOR=18

LABEL org.opencontainers.image.title="musicmeta-offline" \
      org.opencontainers.image.description="Offline-Spiegel fuer AcoustID (ein Container: Waechter, API, Postgres, acoustid-index)" \
      org.opencontainers.image.licenses="MIT AND GPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/shares92/musicmeta-offline"
# Eingebackene Fremdkomponenten, sichtbar am Image (v2 §12) und Teil des
# GPL-Quellangebots (E7).
LABEL org.musicmeta.postgresql.major="18" \
      org.musicmeta.acoustid-index.source="https://github.com/acoustid/acoustid-index" \
      org.musicmeta.acoustid-index.commit="${ACOUSTID_INDEX_COMMIT}" \
      org.musicmeta.acoustid-index.license="GPL-3.0-or-later" \
      org.musicmeta.acoustid-index.source-archive="/usr/share/musicmeta/acoustid-index-source.tar.gz"

# `MMO_PG_MAJOR`/`MMO_INDEX_COMMIT` beschreiben das Artefakt, nicht den
# Betrieb: der Waechter prueft damit den Versions-Drift (E14) und weist beide
# in `/status` aus (v2 §12). Sie stehen als Env **und** als OCI-Label — das
# Label ist von aussen lesbar, die Env von innen.
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/opt/supervisor/bin:${PATH}" \
    MMO_PG_MAJOR=18 \
    MMO_INDEX_COMMIT=${ACOUSTID_INDEX_COMMIT}

# Feste UIDs, bevor Postgres installiert wird: Bind-Mounts auf Unraid-Shares
# brauchen berechenbare Eigentuemer (R14). 6081 ist die UID des
# acoustid-index-Upstream-Images — ein v1-Index-Verzeichnis passt damit ohne
# chown. NICHT 99:100 (nobody/users) verwenden.
#
# Vier Identitaeten, drei davon unprivilegiert:
#
#   postgres  999        die Datenbank
#   acoustid  6081       der Suchindex (UID des Upstream-Images)
#   api       6082:6080  der API-Dienst — er verarbeitet als einziger
#                        Fremdeingaben und laeuft deshalb NICHT als root
#   musicmeta      6080  Gruppe fuer genau die Dateien unter /config, die
#                        der API-Dienst lesen muss (config.yaml, das
#                        Datenbank-Passwort). Sie ist seine **primaere**
#                        Gruppe: damit haengt der Zugriff nicht an
#                        Supplementary-Groups des Supervisors.
RUN groupadd --system --gid 999 postgres \
    && useradd --system --uid 999 --gid 999 --home-dir /var/lib/postgresql \
        --shell /bin/sh --comment "PostgreSQL" postgres \
    && groupadd --gid 6081 acoustid \
    && useradd --uid 6081 --gid 6081 --home-dir /nonexistent \
        --shell /usr/sbin/nologin --comment "acoustid-index" acoustid \
    && groupadd --gid 6080 musicmeta \
    && useradd --uid 6082 --gid 6080 --home-dir /nonexistent \
        --shell /usr/sbin/nologin --comment "musicmeta API" api \
    && mkdir -p /var/lib/postgresql \
    && chown postgres:postgres /var/lib/postgresql

# Kein automatisches Cluster von postgresql-common: das Datenverzeichnis
# liegt auf einem Mount und wird vom Entrypoint angelegt.
RUN mkdir -p /etc/postgresql-common \
    && echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.conf

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
        "https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "postgresql-${PG_MAJOR}" "postgresql-client-${PG_MAJOR}" \
        gosu tini \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=app-build /app /app
COPY --from=supervisor-build /opt/supervisor /opt/supervisor
COPY --from=fpindex-build /src/zig-out/bin/fpindex /usr/local/bin/fpindex
COPY --from=fpindex-build /src/acoustid-index-source.tar.gz /usr/share/musicmeta/
COPY THIRD-PARTY-NOTICES.md LICENSE /usr/share/musicmeta/
COPY supervisor/supervisord.conf supervisor/supervisord.dev.conf /etc/supervisor/
COPY supervisor/entrypoint.sh /usr/local/bin/mmo-entrypoint
COPY supervisor/mmo-postgres supervisor/mmo-fpindex /usr/local/bin/
RUN chmod 0755 /usr/local/bin/mmo-entrypoint /usr/local/bin/mmo-postgres \
        /usr/local/bin/mmo-fpindex /usr/local/bin/fpindex

# Die sechs Mountpunkte aus v2 §3 (+ /backup, K9). Sie werden hier nur
# angelegt; die Zuordnung macht docker-compose.yml (Bind-Mounts, E13).
RUN mkdir -p /config /data/db /index /import /backup

# NICHT /app: dort liegt das Workspace-Verzeichnis `shared/`, das als
# Namespace-Paket den installierten Import-Namen `shared` verdecken wuerde
# (LEARNINGS "Mehrere gleichnamige Python-Pakete kollidieren im venv").
WORKDIR /

# Nur der Waechter-Port wird veroeffentlicht; Postgres (5432), Index (6081)
# und API (8081) sind containerintern und lauschen auf dem Loopback.
EXPOSE 8080

# tini als PID 1 (E1): raeumt Zombies ab und leitet Signale weiter — beides
# braucht supervisord, das selbst kein Init ist.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/mmo-entrypoint"]
CMD ["/opt/supervisor/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
