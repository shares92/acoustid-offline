# API-Image von acoustid-offline (ARCHITECTURE §10).
#
# Bauen aus dem Repo-Wurzelverzeichnis (der Build-Kontext ist das Repo, weil
# die API das Workspace-Paket `shared` braucht):
#
#   docker compose build api
#
# Der Dienst laeuft dauerhaft, solange der Stack wach ist, und hat KEINEN
# veroeffentlichten Port: davor sitzt der Waechter als Proxy und setzt Auth
# und Rate-Limit durch (ARCHITECTURE §3, §7).
#
# Basis ist das offizielle uv-Image auf python:3.14-slim — dieselbe
# Werkzeugkette wie in CI und lokal, damit im Container genau die Versionen
# aus uv.lock landen (gleiches Muster wie importer/Dockerfile).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# UV_COMPILE_BYTECODE: der Prozess wird bei jedem Wecken des Stacks neu
# gestartet, Startzeit zaehlt mehr als Imagegroesse. UV_PYTHON_DOWNLOADS=never:
# das Image bringt sein Python schon mit, es soll keins nachladen.
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
RUN uv sync --frozen --no-dev --no-install-workspace --package acoustid-offline-api

# Jetzt der eigene Code: nur die beiden Pakete, die der Dienst wirklich braucht.
COPY shared/ shared/
COPY api/ api/
RUN uv sync --frozen --no-dev --package acoustid-offline-api

# NICHT /app: dort liegt das Workspace-Verzeichnis `shared/`, das als
# Namespace-Paket den installierten Import-Namen `shared` verdecken wuerde
# (LEARNINGS „Mehrere gleichnamige Python-Pakete kollidieren im venv").
WORKDIR /

# Containerinterner Port; wird bewusst nicht veroeffentlicht.
EXPOSE 8080

CMD ["/app/.venv/bin/python", "-m", "acoustid_api"]
