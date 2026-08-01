# Waechter-Image von acoustid-offline (ARCHITECTURE §10).
#
# Bauen aus dem Repo-Wurzelverzeichnis (der Build-Kontext ist das Repo, weil
# der Waechter das Workspace-Paket `shared` braucht):
#
#   docker compose -f docker-compose.watchdog.yml build
#
# Anders als api und importer laeuft dieser Dienst DAUERHAFT und auf dem
# SSD-Cache-Pool — er ist der einzige, der einen Port veroeffentlicht
# (ARCHITECTURE §3): API-Proxy, /status, /metrics und die Admin-UI unter
# /admin teilen sich AOFF_PORT.
#
# Basis ist das offizielle uv-Image auf python:3.14-slim — dieselbe
# Werkzeugkette wie in CI und lokal, damit im Container genau die Versionen
# aus uv.lock landen (gleiches Muster wie api/Dockerfile).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# UV_COMPILE_BYTECODE: der Waechter startet selten, laeuft aber lange — die
# Startzeit zaehlt trotzdem mehr als ein paar MB Image. UV_PYTHON_DOWNLOADS=never:
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
RUN uv sync --frozen --no-dev --no-install-workspace --package acoustid-offline-watchdog

# Jetzt der eigene Code: nur die beiden Pakete, die der Dienst wirklich braucht.
COPY shared/ shared/
COPY watchdog/ watchdog/
RUN uv sync --frozen --no-dev --package acoustid-offline-watchdog

# NICHT /app: dort liegt das Workspace-Verzeichnis `shared/`, das als
# Namespace-Paket den installierten Import-Namen `shared` verdecken wuerde
# (LEARNINGS „Mehrere gleichnamige Python-Pakete kollidieren im venv").
WORKDIR /

# Vorgabewert; der tatsaechliche Port kommt aus AOFF_PORT und wird von
# Compose veroeffentlicht.
EXPOSE 8080

CMD ["/app/.venv/bin/python", "-m", "acoustid_watchdog"]
