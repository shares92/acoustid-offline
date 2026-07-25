# acoustid-offline

Selbst gehostete, offline-fähige [AcoustID](https://acoustid.org)-Instanz als
Docker-Stack: Audio-Fingerprint-Lookup (Chromaprint → AcoustID-UUID →
MusicBrainz-Recording inkl. Metadaten) ohne Abhängigkeit von
`api.acoustid.org`.

Ziele (ARCHITECTURE.md §1):

- Standard-Clients (DroppedNeedle, Picard, beets per URL-Umbiegung) bekommen
  auf `/v2/lookup` **API-kompatible** Antworten.
- Der Stack **schläft im Normalzustand** vollständig (die Array-Platten dürfen
  herunterfahren); nur der Wächter läuft dauerhaft auf dem SSD-Cache.
- Der Datenbestand **aktualisiert sich täglich** per Delta-Import, inkl.
  Selbst-Wecken und Wieder-Einschlafen.
- Läuft auf jedem Docker-Host; Referenz-Deployment ist Unraid.

## Status

**In Entwicklung** — der Stack ist noch nicht lauffähig. Aktueller
Stand, Phasenplan und Fortschritt: [PROGRESS.md](PROGRESS.md)
(Statuszeile ganz oben).

Eine Setup-Anleitung (Unraid und generisch, Bootstrap des Datenbestands)
folgt in späteren Phasen, sobald Compose-Dateien und Images existieren.

## Architektur in einem Satz

Ein Repo, fünf Container, zwei Compose-Dateien: ein immer laufender **Wächter**
(Proxy mit Weck-Logik, Scheduler, Admin-UI, Lookup-Cache) weckt bei Bedarf den
schlafenden **Stack** aus API-Service, Importer-Job, PostgreSQL und
acoustid-index — Details, Datenmodell und Invarianten in
[ARCHITECTURE.md](ARCHITECTURE.md), Grundsatzentscheidungen in
[DECISIONS.md](DECISIONS.md).

## Projektstruktur

```
acoustid-offline/
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

Der Importer läuft als One-Shot-Job (`docker compose --profile job run
--rm importer`); Aufrufe, Exit-Codes und das Report-Format stehen in
[docs/importer-job.md](docs/importer-job.md). Für den Probelauf auf der
Unraid-Referenzhardware gibt es eine eigene Anleitung:
[docs/probelauf-unraid.md](docs/probelauf-unraid.md). Die vollständige
Bootstrap-Anleitung folgt in Phase 29.

## Lizenz

**Code:** MIT — siehe [LICENSE](LICENSE).

**Datenbestand:** Die öffentliche AcoustID-Datenbank steht unter
[CC BY-SA 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/); das
MusicBrainz-AcoustID-Mapping ist Public Domain (CC0). Dieses Repo enthält
**keine** AcoustID-Daten und verteilt den Datenbestand nicht weiter — jede
Instanz lädt die Delta-Dateien selbst von
[data.acoustid.org](https://data.acoustid.org). Wer Daten aus einer eigenen
Instanz weitergibt, muss die Bedingungen der CC BY-SA 3.0 einhalten
(Namensnennung, Weitergabe unter gleichen Bedingungen).
