# musicmeta-offline

Selbst gehostetes Offline-Gateway für Musik-Metadaten als **ein**
Docker-Container. Gebaut ist bisher der
[AcoustID](https://acoustid.org)-Teil: Audio-Fingerprint-Lookup
(Chromaprint → AcoustID-UUID → MusicBrainz-Recording inkl. Metadaten) ohne
Abhängigkeit von `api.acoustid.org`. Daneben kommen Discogs, das Cover Art
Archive und TheAudioDB ([docs/HANDOFF.md](docs/HANDOFF.md) §1) — daher der
Name: bis M2 hieß das Projekt **acoustid-offline**
(siehe [Umbenennung](#umbenennung-acoustid-offline--musicmeta-offline)).

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

## Umbenennung: acoustid-offline → musicmeta-offline

Mit der Scope-Erweiterung auf vier Quellen heißt das Projekt seit M2
**musicmeta-offline** ([docs/HANDOFF.md](docs/HANDOFF.md) §16). Umbenannt
sind Repo, Image, der Env-Prefix (`AOFF_` → `MMO_`) und die
Config-Schlüssel ([ARCHITECTURE.md](ARCHITECTURE.md) §6).

**Für Betreiber ändert sich nichts abrupt:** Alte `AOFF_`-Variablen und
alte `config.yaml`-Schlüssel werden **eine Release-Runde** weitergelesen
und melden sich dabei mit einer Warnung, die den neuen Namen nennt; die
`config.yaml` schreibt der Wächter beim ersten Start einmalig um. Ein
Upgrade bricht also nichts — umstellen sollte man trotzdem bei der
Gelegenheit, denn danach fallen die alten Namen ersatzlos weg.

**Reihenfolge der Umstellung** — sie ist bindend, nicht Geschmackssache:

1. **Repo-Inhalte zuerst:** Paketnamen, LICENSE-Zeile, `.env.example`,
   Compose/CI/Release-Workflow, Doku, Badge- und Clone-URLs. Erledigt in
   M2; ab hier zeigt alles im Repo auf `shares92/musicmeta-offline`.
2. **Dann der GitHub-Rename** `shares92/acoustid-offline` →
   `shares92/musicmeta-offline`. GitHub legt dabei eine Weiterleitung an,
   alte Klon-URLs und Links funktionieren also weiter. **Den alten Namen
   nie neu belegen:** ein neues Repo unter `shares92/acoustid-offline`
   schaltet die Weiterleitung sofort ab und lässt jeden Alt-Link ins Leere
   zeigen.
3. **Lokale Klone nachziehen:**
   ```bash
   git remote set-url origin https://github.com/shares92/musicmeta-offline.git
   ```
   Die Weiterleitung erledigt es zwar auch — aber nur, solange sie steht.
4. **GHCR-Paket beim ersten Push von Hand auf öffentlich stellen.** GHCR
   legt ein neues Paket **privat** an, und ein privates Image kann niemand
   ziehen; der erste Release liefe sonst ins Leere.
5. **Alte `acoustid-offline-*`-Pakete als „eingestellt" markieren — nie
   löschen** (DECISIONS E16): bestehende Installationen ziehen sie noch.
6. **Der erste `v*`-Tag kommt NACH dem Rename.** `release.yml` erzwingt
   das mit einem Guard, der den Lauf abbricht, solange
   `github.repository` noch auf den alten Namen zeigt: die Image-Namen
   leiten sich aus `github.repository` ab, ein Tag davor erzeugte also ein
   Image unter dem alten Paketnamen — während die `docker-compose.yml`
   längst `ghcr.io/shares92/musicmeta-offline` zieht. Herauskäme ein
   Release, das niemand findet.

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
| `/backup` | Array | Sicherungen ([docs/backup-restore.md](docs/backup-restore.md)) |

Das Docker-Image selbst gehört ebenfalls auf den Cache — sonst hält der
laufende Container das Array wach.

Der Container-Healthcheck fragt `GET /status`, und das weckt garantiert
nichts: **`unhealthy` heißt „der Wächter ist tot", nicht „das System
schläft"**. Schlafen ist der Gutzustand.

## Projektstruktur

```
musicmeta-offline/
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
MMO_DB_HOST=127.0.0.1 MMO_DB_PASSWORD=test-wegwerf-passwort \
  MMO_INDEX_URL=http://127.0.0.1:6081 uv run pytest --integration=require
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

## Täglicher Betrieb: Zeitplan, Meldungen, Sicherung

Im Normalbetrieb ist nichts zu tun — die Instanz weckt sich zum Termin
selbst, arbeitet und legt sich wieder schlafen. Eingestellt wird das in der
`config.yaml` auf dem `/config`-Mount (ARCHITECTURE §6):

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `acoustid.update.time` | `04:00` | Täglicher Delta-Import (lokale Zeit) |
| `backup.time` / `backup.dir` | `04:45` / leer | Sicherung; **leeres Ziel heißt „aus"** |
| `notify.ntfy.url` | leer | Push-/Webhook-Ziel; leer = aus |
| `notify.smtp.*` | leer | Mailversand; leerer Host = aus |
| `metrics.enabled` | `false` | `GET /metrics` im Prometheus-Format |
| `disk.min_free_gb` | `100` | Reserve, geprüft gegen **jeden** Schreibpfad |

Ein verpasster Termin wird am selben Tag nachgeholt; ein fehlgeschlagener
Lauf wird beim nächsten Zyklus wiederholt (der Stand bleibt resumierbar).
Was der letzte Lauf gemacht hat, steht in `GET /status` unter
`last_update_run`.

Gemeldet wird nur, was Aufmerksamkeit braucht: fehlgeschlagener Import,
knapper Plattenplatz, Stack-Start-Fehler, endgültig aufgegebene
Upstream-Einreichungen und ein Versions-Drift der Datenbank.

Was gesichert wird — und wie man es zurückspielt — steht in
[docs/backup-restore.md](docs/backup-restore.md). Kurz: die eigenen
Einreichungen, der Wächter-Zustand und die Konfiguration. Alles andere
kommt aus den Quellen zurück.

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
