# acoustid-offline

Selbst gehostete, offline-fähige [AcoustID](https://acoustid.org)-Instanz als
**ein** Docker-Container: Audio-Fingerprint-Lookup (Chromaprint →
AcoustID-UUID → MusicBrainz-Recording inkl. Metadaten) ohne Abhängigkeit von
`api.acoustid.org`.

Ziele (ARCHITECTURE.md §1):

- Standard-Clients (DroppedNeedle, Picard, beets per URL-Umbiegung) bekommen
  auf `/v2/lookup` **API-kompatible** Antworten.
- Der Stack **schläft im Normalzustand** (die Array-Platten dürfen
  herunterfahren); wach bleiben nur Wächter und Suchindex — beide auf dem
  SSD-Cache.
- Der Datenbestand **aktualisiert sich täglich** per Delta-Import, inkl.
  Selbst-Wecken und Wieder-Einschlafen.
- Läuft auf jedem Docker-Host; Referenz-Deployment ist Unraid.

## Status

**In Entwicklung** — der Stack ist noch nicht lauffähig. Aktueller
Stand, Phasenplan und Fortschritt: [PROGRESS.md](PROGRESS.md)
(Statuszeile ganz oben).

Eine vollständige Setup-Anleitung (Unraid und generisch, Bootstrap des
Datenbestands) folgt zum Release. Wer von einer v1-Installation kommt:
[docs/migration-v1-v2.md](docs/migration-v1-v2.md).

## Architektur in einem Satz

Ein Repo, **ein Image, ein Container**: darin steuert `supervisord` (unter
`tini` als PID 1) vier Prozesse — ein immer laufender **Wächter** (Proxy mit
Weck-Logik, Scheduler, Admin-UI, Lookup-Cache) weckt bei Bedarf die
schlafenden **Dienste** PostgreSQL und API-Service. Der Suchindex bleibt
resident, weil sein Kaltstart den kompletten Index liest. Details,
Datenmodell und Invarianten in [ARCHITECTURE.md](ARCHITECTURE.md),
Grundsatzentscheidungen in [DECISIONS.md](DECISIONS.md).

### Volumes: was auf den Cache gehört und was aufs Array

| Mount | Ablage | Inhalt |
|---|---|---|
| `/config` | **Cache** | `config.yaml`, SQLite, Lookup-Cache, Logs, DB-Passwort |
| `/index` | **Cache** | Suchindex (~70 GB einplanen) |
| `/data/db` | Array | PostgreSQL |
| `/import` | Array | Dump-Downloads |
| `/backup` | Array | Sicherungen (ab der Backup-Phase) |

Das Docker-Image selbst gehört ebenfalls auf den Cache — sonst hält der
laufende Container das Array wach.

Der Container-Healthcheck fragt `GET /status`, und das weckt garantiert
nichts: **`unhealthy` heißt „der Wächter ist tot", nicht „das System
schläft"**. Schlafen ist der Gutzustand.

## Projektstruktur

```
acoustid-offline/
├── Dockerfile            # das eine Image: App + PostgreSQL + acoustid-index
├── docker-compose.yml    # der eine Service
├── supervisor/           # Prozessdefinitionen und Container-Vorlauf
├── shared/shared/        # gemeinsames Paket: Config, Modelle, Logging  → import shared
├── api/app/              # API-Service (/v2/lookup, /v2/submit, Batch)  → import acoustid_api
├── importer/app/         # Delta-Import, Index-Feed, Backup             → import acoustid_importer
├── watchdog/app/         # Wächter: Proxy, Scheduler, Admin-UI          → import acoustid_watchdog
│   ├── templates/        # Jinja2-Templates der Admin-UI
│   └── static/           # CSS/JS (HTMX), kein Frontend-Build
├── unraid/               # Unraid-Community-App-Template (XML)
├── tests/                # paketübergreifende Tests + Fixtures
├── docs/                 # HANDOFF, DESIGN_HANDOFF, Recherche-Reports
└── .github/workflows/    # CI (Lint + Tests)
```

Jedes Service-Verzeichnis ist ein eigenes Paket im uv-Workspace. Die
Import-Namen sind bewusst eindeutig (`acoustid_api` statt `app`), weil alle
Pakete in ein gemeinsames venv installiert werden; die Verzeichnisnamen folgen
ARCHITECTURE.md §10.

## Entwicklung

Voraussetzung: [uv](https://docs.astral.sh/uv/) und Python 3.14.

```bash
uv sync --all-packages     # venv anlegen, alle vier Pakete editierbar installieren
uv run ruff check .        # Lint
uv run ruff format .       # Format (CI prüft mit --check)
uv run pytest              # Tests aller Pakete
```

Test-Fixtures des AcoustID-Dumps liegen nicht im Repo und werden bei Bedarf
nachgeladen:

```bash
uv run python tests/fixtures/fetch_fixtures.py
```

Siehe [tests/fixtures/acoustid-dumps/README.md](tests/fixtures/acoustid-dumps/README.md).

Integrationstests brauchen PostgreSQL und den Suchindex. Beide kommen aus
**demselben Image** wie der Betrieb — so testet die Suite genau das
Artefakt, das ausgeliefert wird:

```bash
docker compose -f tests/docker-compose.test.yml up -d --build --wait
AOFF_DB_HOST=127.0.0.1 AOFF_DB_PASSWORD=test-wegwerf-passwort \
  AOFF_INDEX_URL=http://127.0.0.1:6081 uv run pytest --integration=require
docker compose -f tests/docker-compose.test.yml down -v
```

Der E2E-Test fährt den echten Container hoch (`uv run pytest
tests/test_wake_e2e.py --compose`); auf Apple Silicon dafür colima mit
`--vz-rosetta` starten — das Image ist amd64-only.

Der Importer läuft als Prozess im Container (`docker compose exec app
/app/.venv/bin/python -m acoustid_importer`); Aufrufe, Exit-Codes und das
Report-Format stehen in [docs/importer-job.md](docs/importer-job.md). Für
den Probelauf auf der Unraid-Referenzhardware gibt es eine eigene Anleitung:
[docs/probelauf-unraid.md](docs/probelauf-unraid.md).

## Lizenz

**Code:** MIT — siehe [LICENSE](LICENSE).

**Eingebackene Fremdkomponenten:** Das Auslieferungs-Image enthält
PostgreSQL, `supervisord`, `tini` und **acoustid-index** (`fpindex`). Der
Suchindex steht unter der **GPL-3.0-or-later**; sein vollständiger
Quelltext wird mit dem Image ausgeliefert
(`/usr/share/musicmeta/acoustid-index-source.tar.gz`, Commit im
Image-Label). Alle Angaben und der Bezugsweg:
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Die Programme laufen als eigenständige Prozesse und sprechen ausschließlich
über Netzwerkprotokolle mit dem eigenen Code — es entsteht kein abgeleitetes
Werk; die Weitergabe der Binaries erzeugt trotzdem Pflichten, denen die
NOTICES-Datei nachkommt.

**Datenbestand:** Die öffentliche AcoustID-Datenbank steht unter
[CC BY-SA 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/); das
MusicBrainz-AcoustID-Mapping ist Public Domain (CC0). Dieses Repo enthält
**keine** AcoustID-Daten und verteilt den Datenbestand nicht weiter — jede
Instanz lädt die Delta-Dateien selbst von
[data.acoustid.org](https://data.acoustid.org). Wer Daten aus einer eigenen
Instanz weitergibt, muss die Bedingungen der CC BY-SA 3.0 einhalten
(Namensnennung, Weitergabe unter gleichen Bedingungen).
