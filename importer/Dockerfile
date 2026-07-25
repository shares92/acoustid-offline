# Importer-Image von acoustid-offline (One-Shot-Job, ARCHITECTURE §10).
#
# Bauen aus dem Repo-Wurzelverzeichnis (der Build-Kontext ist das Repo, weil
# der Importer das Workspace-Paket `shared` braucht):
#
#   docker compose --profile job build importer
#
# Starten immer ueber das Compose-Profil `job`; Argumente gehen unveraendert
# an `python -m acoustid_importer` (siehe docs/importer-job.md):
#
#   docker compose --profile job run --rm importer --mode bootstrap
#
# Basis ist das offizielle uv-Image auf python:3.14-slim — dieselbe
# Werkzeugkette wie in CI und lokal, damit im Container genau die Versionen
# aus uv.lock landen.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# UV_COMPILE_BYTECODE: der Job startet einmal taeglich, Startzeit zaehlt mehr
# als Imagegroesse. UV_PYTHON_DOWNLOADS=never: das Image bringt sein Python
# schon mit, es soll keins nachladen.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Erst die Metadaten aller Workspace-Member (uv braucht sie, um den
# Workspace ueberhaupt aufzuloesen), dann die Abhaengigkeiten — so bleibt
# diese Schicht im Cache, solange sich uv.lock nicht aendert.
COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY api/pyproject.toml api/
COPY importer/pyproject.toml importer/
COPY watchdog/pyproject.toml watchdog/
RUN uv sync --frozen --no-dev --no-install-workspace --package acoustid-offline-importer

# Jetzt der eigene Code: nur die beiden Pakete, die der Job wirklich braucht.
COPY shared/ shared/
COPY importer/ importer/
RUN uv sync --frozen --no-dev --package acoustid-offline-importer

# NICHT /app: dort liegt das Workspace-Verzeichnis `shared/`, das als
# Namespace-Paket den installierten Import-Namen `shared` verdecken wuerde
# (LEARNINGS „Mehrere gleichnamige Python-Pakete kollidieren im venv").
WORKDIR /

# Ein Job, ein Prozess, ein Exit-Code (docs/importer-job.md).
ENTRYPOINT ["/app/.venv/bin/python", "-m", "acoustid_importer"]
