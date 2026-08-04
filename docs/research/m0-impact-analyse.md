# M0 — Impact-Analyse: HANDOFF v2 gegen den v1-Bestand

Stand: 2026-08-04. Grundlage: `docs/HANDOFF.md` (v2 vom 03.08.2026,
„musicmeta-offline"; v1 archiviert als docs/archive/HANDOFF-v1.md — zur
Analysezeit lag v2 noch als `HANDOFF.md` im Root), dort §16-M0: „ARCHITECTURE.md/PROGRESS.md
gegen diesen Anhang prüfen … Ergebnis: konkrete Betroffenheitsliste + ggf.
Anpassung der Phasenfolge. Erst danach weiterbauen."

Methode: vier parallele, voneinander unabhängige Analysen (Wächter-Code;
API/Importer/Shared; Infra/CI/Doku; Supervisor-Recherche mit Websuche und
Quellenliste), Befunde mit Datei:Zeile am Code belegt; die
entscheidungstreibenden HOCH-Befunde vom Orchestrator selbst am Code
nachgeprüft. Zweit-Review des Entwurfs: Opus (13 Findings) und GPT 5.6
(12 Findings), parallel und blind; alle Konsens- und verifizierten
Einzel-Findings sind in diese Fassung eingearbeitet (u. a. E10 neu
entschieden, E12 als bewusste v2-Abweichung gekennzeichnet, fehlende
Kante `ready→error`, Backup-Mount-Lücke, Erstpasswort-Logweg).

**Status: Entwurf zur Betreiber-Entscheidung.** Es wurde nichts umgebaut,
keine Steuerungsdatei umgeschrieben, kein Code geändert.

---

## 1. Kernbefund

1. **Der Umbau des Bestands-Codes ist klein — der Neubau ist der
   eigentliche M1-Aufwand.** Die Multi-Container-Annahme steckt im
   Wächter in drei Modulen (`docker.py`, Teile von `wake.py`,
   `StatePoller`): ca. 530 von ~5.470 Zeilen App-Code (≈ 10 % des
   Bestands). In `api/`, `importer/` und `shared/` gibt es keine Zeile
   Docker-Steuerung — nur Defaults, Docstrings und zwei harte
   Portfestlegungen. Dazu kommt aber Neubau, der in keiner
   Bestandszeile steckt: supervisord-Konfiguration + Entrypoint,
   `process.py`, ein Multi-Stage-Dockerfile (PG 18 + fpindex aus
   Quelle), Volume-Migrationsrezept, Schema-Migration,
   `FakeSupervisor`, E2E-Portierung. Die Naht selbst
   (`StackController`: `start()`/`stop()`/`all_running()`,
   `watchdog/app/wake.py:184–215`) ist schmal — dort ist es ein
   Adapter-Tausch, kein Architekturbruch.
2. **Der Test-Umbau ist größer als der Code-Umbau.** Rund 1.100 von
   ~4.280 Wächter-Testzeilen (≈ 26 %) hängen an der Docker-Attrappe
   `FakeDaemon`; `tests/test_wake_e2e.py` (472 Z., ~60 % neu) und
   `tests/test_repo_layout.py` brechen vollständig (Absicht — beide
   halten die Struktur fest; M1-DoD führt sie als „Umbau, nicht
   Umbenennung"). Schlüsselarbeit ist ein `FakeSupervisor` mit gleichem
   Zuschnitt — danach sind die meisten Testdateien Umbenennungen.
3. **Der v2-Migrationsplan (§16) hat eine Lücke.** Er listet unter
   „weiter gültig": Notifications, Backup, Admin-UI-Gerüst — die
   **existieren nicht** (fertig sind Phasen 0–18; die alten Phasen
   19–22 und 23–27 sind ungebaut, PROGRESS.md:81–84). M8 („Admin-UI-
   **Erweiterung**") unterstellt eine UI, die es nicht gibt. Der Plan
   braucht eine eingeschobene Phase **M2.5** (Scheduler, Notify,
   Backup, Metrics auf Supervisor-Basis) und M8 = „Admin-UI komplett".
4. **v2 enthält Widersprüche und Lücken, die Betreiber-Entscheide
   brauchen** (Details §3): amd64-only-Index vs. Multi-Arch-Zusage;
   `/data/index` auf dem Array vs. messdatenbasierter Cache-Entscheid;
   GPL-Pflichten beim Einbacken; kein Volume-Ziel für `backup.dir`;
   dazu zwei bewusste Abweichungsvorschläge dieser Analyse (Index
   resident E12, pg_upgrade-Pfad E14), die als solche gekennzeichnet
   sind.
5. **Supervisor-Frage (§5/§14.6) ist entscheidungsreif.** Code-Analyse
   und Recherche kommen unabhängig zum selben Ergebnis: **supervisord
   (+ `tini` als PID 1)** — einziger Kandidat mit echter
   Prozess-Steuerungs-API (XML-RPC über Unix-Socket); die Faults
   `ALREADY_STARTED`/`NOT_RUNNING` bilden die Idempotenz-Semantik von
   `docker.py` exakt ab. Vorbehalt (M1b-Prüfpunkt): supervisord ist
   selbst Python; die Verträglichkeit der aktuellen Version (4.3.0,
   PyPI weist Support bis Py 3.13 aus) mit dem projektgepinnten
   Python 3.14 ist zu verifizieren — sonst eigener Interpreter im
   Image oder Neubewertung. **Jobs laufen NICHT über supervisord**
   (E10): `[program:*]` hat statisches `command=`, `startProcess()`
   kennt keine Argumente — der Importer braucht aber Per-Lauf-Parameter
   (`--mode`, `--end-date`, `--report`, …; `importer/app/__main__.py`).

---

## 2. Betroffenheitsliste (konsolidiert)

Vollständige zeilengenaue Tabellen liegen in den vier Einzelanalysen
(Session 2026-08-04); hier die tragenden Posten.

### 2.1 Wächter (`watchdog/`) — der Code-Hotspot

| Stelle | Annahme | v2-Änderung | Aufwand |
|---|---|---|---|
| `app/docker.py` (ganz, 281 Z.) | Docker-Engine-API über `/var/run/docker.sock` | Ersatzmodul `process.py` (`SupervisorClient`, XML-RPC), 1:1-Zuschnitt `inspect`/`start`/`stop` (+ `signal`, `states`) | M |
| `app/wake.py:78–93` | `STACK_CONTAINERS`, `API_HEALTH_URL`/`API_BASE_URL` = Compose-DNS `acoustid-api:8080` | Prozessnamen; Adressen auf Loopback und in `EnvSettings` statt Modulkonstanten | S |
| `app/wake.py:173–215` `StackController` | ruft Docker je Container; Compose-`depends_on` sicherte die Reihenfolge | `ServiceGroupController`: **sequenzieller** Start PG → Index → API mit Readiness-Gate je Prozess (pg_isready/psycopg-Connect; Index TCP + `/:index/_health`; API `/_health`) — die API bricht ihren Start nach 30 s ohne DB ab (`api/app/service.py:120`), ein Gruppen-Start ohne Gates liefe in FATAL. Vorher `Protocol` einziehen (`wake.py:222` typisiert konkret) | M |
| `app/wake.py:486–562` `refresh()`/`observe()` | „kein Container läuft" = schlafend (eindeutig gutartig) | Bedeutungswechsel: `STOPPED` kann auch Absturz-/`FATAL`-Folge sein → gewollter Zustand nötig, sonst maskiert ein Absturz sich als Schlaf | M |
| `app/state.py` | Kante `ready→error` ist **ausdrücklich verboten** („Fehlerzustand nur bei gescheitertem Start/Stopp", state.py-Docstring; `ALLOWED_TRANSITIONS`) — in v1 korrekt, weil ein von Hand gestoppter Container gutartig war | **Neue Kante `ready→error`** (Prozessabsturz im Betrieb) oder Soll-/Ist-Trennung; Übergangstabelle, Docstring und die 25-Paare-Tests ändern sich. Die Tabelle ist also NICHT kantengleich übernehmbar | M |
| `app/lifecycle.py:256–296` `StatePoller` | Hand-Stopp/-Start-Erkennung via Docker | Absturz-/Autorestart-Erkennung via `getAllProcessInfo()`-**Polling** (bleibt In-Process). supervisord-Eventlistener sind eigene, von supervisord gespawnte Prozesse mit stdin/stdout-Protokoll — ein Push-Kanal bräuchte einen Brückenprozess und ist optionaler Ausbau, kein M1-Bestandteil | M |
| `app/service.py:71,164–173` | verdrahtet `DockerClient`/`StackController` | Supervisor-Client verdrahten | S |
| `app/store.py:109–124` + `app/runs.py:45` | `update_run` mit `CHECK (kind IN ('update','backup'))`, `RunKind` = update/backup | v2 §8 verlangt sechs Job-Typen (acoustid-delta, discogs-dump, caa-crawl, nachzügler, backup, queue-send) → SQLite-Migration + Enum-Erweiterung (M2.5/M3) | S–M |
| `app/status.py:59–64` | `/status` = `stack`/`data`/`last_update_run` (nur AcoustID) | **additiv** erweitern: Prozess-Zustände, Datenstände aller Quellen, Crawl-Fortschritt, eingebackene PG-/Index-Versionen (§9/§12). Feld `stack` **behalten** (bestehender Vertrag) — keine Umbenennung | M (verteilt M1–M7) |
| `app/cache.py` | Invalidierung bewusst **vollständig** (DECISIONS 2026-08-01) | v2 §10.6 verlangt quellen-**selektive** Kohärenz (TADB nur manuell) → Invalidierung nach Quelle bzw. getrennte Cache-Räume; gekippter v1-Entscheid, in DECISIONS nachzuziehen (M3+) | M |
| Tests: `watchdog_stubs.py:54–121` `FakeDaemon` | Engine-Attrappe in fast jedem Aufbau | `FakeSupervisor` gleichen Zuschnitts — Schlüsselarbeit, danach meist Umbenennungen | M |
| `tests/test_wake_e2e.py` (Marker `compose`) | zwei Compose-Dateien, `docker inspect`/`compose stop` je Container | ~60 % neu (Prozesszustände via `supervisorctl status`); die fünf Nachweise bleiben fachlich gültig | L |

Container-agnostisch und **für den M1-Umbau unverändert**: `auth.py`,
`ratelimit.py`, `events.py`, `admin.py`, `config_store.py`, `reload.py`,
`proxy.py` (Mechanik; `base_url` ist Konstruktorparameter), die
Pipeline-Reihenfolge Rate-Limit → Auth → Cache → Wecken in `main.py`,
`lifecycle.py` bis auf den Poller. Die Invarianten-Tests (Socket-Verbot
in `/status`, Cache-Tripwire) decken den Supervisor-Fall automatisch ab.
**Aber:** „unverändert" gilt nur für M1 — die Scope-Erweiterung
(M3–M7) baut genau diese Schichten aus: Proxy-Routing für
`/v1`/`/discogs`/`/caa`/`/tadb` (heute nur `/v2/{path}`,
`main.py:157–159`), Auth einheitlich über alle Pfadfamilien mit
Fehlerformat der jeweils nachgebildeten API (§9; heute AcoustID-Codes
mit `client`-Parameter-Bindung), Cache quellenübergreifend (§6.10;
heute nur `/v2/lookup` mit `status: ok`-Prüfung). Diese Posten stehen
im Phasenplan (§4).

### 2.2 API / Importer / Shared — Defaults, zwei Ports, ein fehlendes Schema

| Stelle | Befund | v2-Änderung | Aufwand |
|---|---|---|---|
| `api/app/__main__.py:21,32` | API bindet fest `0.0.0.0:8080` — kollidiert im Ein-Container mit dem Wächter-Port 8080 (`EADDRINUSE` erst beim ersten Wecken) | neuer Bootstrap-Wert `MMO_API_PORT` (z. B. 8081), Bind `127.0.0.1` | M |
| `shared/shared/env.py:44` | `ENV_PREFIX = "AOFF_"` | → `MMO_` (ein Code-Punkt; alte Variablen werden danach **ungesehen** ignoriert → Übergangslesen nötig) | S |
| `shared/shared/env.py:46,86,89,92,102` | `/data` als Wächter-Datenverzeichnis; `db_host="acoustid-db"`, `index_url="http://acoustid-index:6081"` | `/config` (Cache!), `/import`; Loopback-Defaults. **`/data` ist in v2 das Array** — ohne Umzug lägen SQLite/Keys/Lookup-Cache auf Spindeln und das Array schläft nie | S |
| `shared/shared/db/sql/` (13 Migrationen) | **kein** `CREATE SCHEMA` — alle Tabellen liegen unqualifiziert in `public`; „Schema `acoustid`" aus §8/§16 existiert real nicht | neue Migration `CREATE SCHEMA acoustid` + `ALTER TABLE … SET SCHEMA` + `search_path` je Verbindung (Muster: `mb/client.py`); Alt-SQL nie editieren (`MigrationDriftError`, `migrations.py:264–276`) | M |
| `shared/shared/config.py:492–496` | unbekannte Schlüssel → nur Warnung + Default | **stille Config-Amnesie** bei Key-Umbenennung: `submit.mode: off` fiele lautlos auf `local` zurück → AliasChoices + Start-Umschreiber + Test mit v1-config.yaml | M |
| Config-Schema (`config.py:166–342`) | `submit.*`, `update.*` als Top-Level | `acoustid.submit.*`, `acoustid.update.time`; `min_free_gb` Default 50→100; 7 neue Keys (`discogs.*`, `tadb.api_key`, `caa.crawl.*`, `covers.negative_retry_days`, `backup.include_covers`); neue Secrets als `SecretStr` | M |
| `api/app/health.py:20–22` | `/_health` implizit geschützt, weil der Proxy nur `/v2/*` weiterreicht | v2 proxyt fünf Pfadfamilien → **explizite Deny-Regel** im Wächter (einziger Sicherheits-Regress des Pakets) | S |
| `importer/` als Job | bereits „Job mit Exit-Code + Report", POSIX-Signale, atomarer Report; reicher Per-Lauf-Parametersatz (`__main__.py:56–160`) | Container-gebunden nur Entrypoint/Env/Stop-Frist; `--report <Pfad>` wird Pflicht; Stop-Frist-Falle bleibt (SIGKILL statt Exit 8 bei knapper Frist) | S |
| `importer/app/diskguard.py` | prüft heute nur `dump_dir` (DB-Volume war aus dem Container unsichtbar, Docstring `:9–13`) | Ein-Container **hebt die Einschränkung auf**: Guard vor jedem Import-/Crawl-Segment (§10.10) muss **jeden Schreibpfad** prüfen (mehrere Mounts = mehrere Dateisysteme); `require_free_space` je Pfad existiert (`diskguard.py:47`) | M |
| `shared/shared/mb/` | Query-Schicht prüft 17 Tabellen im Schema `musicbrainz` (`queries.py:76–113`); Read-only-Rolle hat nur dieses Schema | Für AcoustID unverändert. M5/M7 brauchen: neue Queries + **DB-GRANTS** auf `cover_art_archive`-Schema und URL-Relationships, Selfcheck-Erweiterung, Verfügbarkeitsprüfung der Spiegel-Daten (v2 §14.5 offen) | M (M5/M7) |
| `api/app/reload.py`, `api/app/health.py` | Marke über geteiltes Volume; Checks db+index | Mechanik funktioniert unverändert (gleiches Dateisystem); nur Pfade/Begründungen | S |

AcoustID-Fachlogik (Lookup/Matching/Submit/Upstream/Batch, Delta-Importer,
fpindex-Client, Fingerprint-Kern): **bestätigt unverändert** — §16
„weiter gültig" stimmt hier. ~750 von ~980 Tests der drei Pakete bleiben
unberührt.

### 2.3 Infra / CI / Doku

| Stelle | v2-Änderung | Aufwand | Phase |
|---|---|---|---|
| `docker-compose.yml` + `docker-compose.watchdog.yml` + 3 Dockerfiles | ersetzt durch **ein** Multi-Stage-Dockerfile + **eine** Compose-Datei (`WORKDIR /`-Regel gegen die Namespace-Falle erhalten; `stop_grace_period` großzügig — Docker killt sonst nach 10 s mitten im geordneten Shutdown) | L | M1 |
| PG-Volume | v1 mountet `/var/lib/postgresql` (Daten unter `18/docker`) → dokumentiertes, **auf Betreiber-Hardware geprobtes** Migrationsrezept nach `/data/db/<major>/` (sonst wäre der 414-GB-Replay neu fällig) | M | M1 |
| Index | Digest-Pin → aus Quelle gebautes Binary (Commit-Pin, GPL-Pflichten E7, UID 6081 als Prozess-User erhalten). **M1b-DoD: mindestens ein CI-Integrationslauf gegen den selbstgebauten fpindex**, nicht nur gegen den Upstream-Digest (`ci.yml:62–63`) — sonst testet die CI ein anderes Artefakt als das Release | M | M1 |
| CI `ci.yml` | Lint+Unit bleibt; Integrationsjob mit Service-Containern **bewusst behalten** (Testinfrastruktur, per DECISIONS-Satz von der Ein-Container-Regel ausgenommen); Bit-Verifikation bleibt 1:1; `release.yml` neu (ein Image → GHCR) | S–M | M1/M9 |
| `tests/docker-compose.test.yml` | Dev-Compose, das aus dem einen Image nur PG+Index startet (sonst sind lokale Integrationstests tot) | M | M1 |
| `.env.example`, README, LICENSE, `unraid/` | Umbenennung + Ein-Container; Unraid-Template neu (1 Container; Mounts s. K9) | M | M2/M9 |
| ARCHITECTURE/PROGRESS/DECISIONS | §3/§4/§6/§8/§10 werden phasenweise ersetzt; **§5.1/§5.2 sind testgekoppelte Sperrzonen** (v2 §8 bestätigt Schema `acoustid` unverändert — Discogs/Covers werden neue Abschnitte daneben) | M | M0–M2 |

---

## 3. Korrekturen an v2 selbst

| # | Widerspruch/Lücke | Beleg | Vorschlag |
|---|---|---|---|
| K1 | §12 Multi-Arch (amd64+arm64) vs. amd64-only `acoustid-index`; unter QEMU hängt das Binary still | docs/research/phase1-acoustid-index.md (Befund 13), LEARNINGS | §12 relativieren: „amd64 verpflichtend, arm64 nach Machbarkeitsnachweis" (Spike: fpindex für aarch64 bauen, 198 Integrationstests dagegen) |
| K2 | §3 legt `/data/index` aufs Array; §16 listet den gekippten Cache-Entscheid nicht | DECISIONS.md:175–186 (Messdaten: HDD kalt 40–80 s = Timeout) | `/data/index` → Cache-Mount (konfigurierbar) |
| K3 | GPL-3.0-Weitergabe des eingebackenen fpindex nicht adressiert; zusätzlich setzt die MIT-Lizenzbegründung (DECISIONS.md:152–159: „GPL-Index nur als separater HTTP-Dienst") die v1-Konstellation voraus | phase1-Bericht: GPL-3.0 | THIRD-PARTY-NOTICES + Quell-/Commit-Pin + Quellangebot; DECISIONS-Lizenzeintrag fortschreiben (getrennte Prozesse über HTTP bleiben getrennte Werke); `pg_acoustid` bleibt strikt draußen |
| K4 | §16 „weiter gültig" listet Ungebautes (Notifications, Backup, Admin-UI-Gerüst); kein Slot für alte Phasen 19–22; M8 „Erweiterung" einer nicht existierenden UI | PROGRESS.md:81–84 | Phase **M2.5** einschieben; M8 = „Admin-UI komplett" |
| K5 | §16 „Tests dieser Module weiter gültig" stimmt für `test_repo_layout.py` und `test_wake_e2e.py` nicht (brechen in M1 vollständig — absichtsvolle Strukturtests) | tests/ | in M1-DoD als „Umbau, nicht Umbenennung" führen; beide grün in neuer Form |
| K6 | §7 lässt `update.min_free_gb` als Ein-Schlüssel-Restsektion neben neuem `acoustid.update.*` zurück | config.py:199–204 | `disk.min_free_gb` (E11) |
| K7 | §7-Tabelle verliert drei real existierende v1-Keys (`index.query_hashes`, `auth.allow_known_client_keys`, `mb.keep_submitted_mbid`) | config.py:161–163,307,322 | alle drei behalten; `index.query_hashes` in M2 nach `acoustid.index.query_hashes` |
| K8 | Ein Container-Healthcheck, aber schlafende Prozesse sind der Normalzustand | — | Healthcheck = `GET /status` (weckt nie); README: „unhealthy = Wächter tot, nicht: System schläft" |
| K9 | §3-Volumeliste (6 Mounts) hat **kein Ziel für `backup.dir`** (§7); ein Backup unter `/config` läge auf demselben Cache-Pool wie die zu sichernde SQLite | HANDOFF.md §3 vs. §7/§6.12 | siebter Mount `/backup` (Array) ins Volume-Layout + Unraid-Template; `backup.dir` leer = aus bleibt Default |
| K10 | **Bewusste Abweichungsvorschläge dieser Analyse** (keine v2-Versehen): E12 (Index resident — §1.2/§3 zählen den Index zur Schlaf-Gruppe) und E14 (pg_upgrade-Verfahren statt „im Entrypoint", §12) | HANDOFF.md:25–27,62–66,417 | je als eigener Betreiber-Entscheid mit Optionen (s. E12/E14); bei Zustimmung v2-Text mitkorrigieren |

---

## 4. Korrigierter Phasenplan (Vorschlag)

§16 nennt seine M-Phasen ausdrücklich „Vorschlag; nach Impact-Analyse
anpassen". Korrigierte Folge:

| Phase | Inhalt | Herkunft |
|---|---|---|
| **M0** | diese Analyse; danach Doku-Sweep (HANDOFF-Umzug, DECISIONS-Einträge, PROGRESS-Neuplan) | §16-M0 |
| **M1a** | Naht ohne Verhaltensänderung: `ProcessGroupController`-Protocol, Fehlerbasisklasse, Adressen/Ports nach `EnvSettings` (inkl. `MMO_API_PORT`-Vorbereitung), `FakeSupervisor` neben `FakeDaemon`. Läuft weiter auf Docker, alle Tests grün — Invariante trivial erfüllt | Split aus M1 |
| **M1b** | Ein-Container-Umbau: supervisord + tini (Py-3.14-Verträglichkeit als erster Prüfpunkt), `process.py`, sequenzieller Start mit Readiness-Gates, neue Zustandskante `ready→error`, ein Dockerfile (PG 18 + fpindex aus Quelle), eine Compose-Datei (`stop_grace_period`), Volume-Layout §3 inkl. K2/K9, dokumentierte + **geprobte** Volume-Migration, E2E-Portierung **in derselben Teilphase**, Dev-Compose, CI-Lauf gegen selbstgebauten fpindex, Versions-Drift-Guard (M1b: Startverweigerung + Log/Eventlog; Notification ab M2.5), Messwerte erheben (PG-Start/Stopp, Index-Kaltstart auf SSD → LEARNINGS) | §16-M1 |
| **M2** | Umbenennung: Repo/Image/Env-Prefix (`AOFF_`→`MMO_` mit Übergangslesen), Config-Keys gemäß Mapping (AliasChoices + Start-Umschreiber), Doku. `/status`-Felder nur **additiv**. **M1+M2 als ein Betreiber-Release** | §16-M2 |
| **M2.5** | alte Phasen 19–22 auf Supervisor-Basis: Scheduler & Update-Zyklus (Fachlogik aus altem Phase-19-Block 1:1, inkl. `invalidate_cache("delta_import")`, `expected_version`-Guard-Entscheid), Job-Ausführung als Wächter-Subprozess (E10) + `update_run`-Migration (neue Job-Typen), Notifications, Backup (Ziel `/backup`, K9), Metrics | alte 19–22 |
| **M3** | **Recherche-Gate** §14.2 (Discogs-XML-Schema, Dump-Rhythmus, Token-Limits), dann Discogs-Spiegel: Schema `discogs`, Dump-Importer (etappenweise/resumierbar), Verfügbarkeits-Check, GET-Subset; Proxy-Routing + Auth + Fehlerformat für `/discogs` | §16-M3 |
| **M4** | **Recherche-Gate** §14.3 (CAA-Bulk-Bezug, Crawl-Rate, URL-Schema), dann Cover-Subsystem: Schema `covers`, **Beschaffungs-Queue-Infrastruktur** (`shared`; je Quelle eine Queue mit Drossel/Backoff, CAA-Queue geteilt Crawler+Lazy — Wiederverwendung von `api/app/upstream.py`-Mustern bewerten), Kette, Normalisierung, CAA-Endpoint, Negativ-Cache; Backup-Erweiterung | §16-M4 |
| **M5** | CAA-Crawler (Verzeichnis aus MB-Spiegel inkl. **neuer Grants** auf `cover_art_archive`, Drossel, Resume, Nachzügler, Idle-Interaktion via `JobSource`, UI-Fortschritt später) | §16-M5 |
| **M6** | **Recherche-Gate** §14.4 (TADB-Key-Klassen, Limits, Cache-ToS), dann TheAudioDB-Proxy-Cache; Cache-Kohärenz quellen-selektiv (TADB nur manuell) | §16-M6 |
| **M7** | Vereinheitlichte `/v1`-API inkl. `mbref` (MBID↔Discogs über URL-Relationships, §14.5-Verfügbarkeitsprüfung); quellenübergreifender Lookup-Cache | §16-M7 |
| **M8** | **Admin-UI komplett** (alte 23–27 + Vier-Quellen-Erweiterung; DESIGN_HANDOFF v2 aus separater Design-Session; klären, was vom abgenommenen v1-Designpaket trägt) | alte 23–27 + §16-M8 |
| **M9** | Abschluss: E2E (Ein-Container, CI-tauglich), Drittclient-/DroppedNeedle-Tests, erster echter Upstream-Lauf, README, Unraid-Template, `release.yml`, arm64-Spike-Ergebnis, XFF-Entscheid | alte 28–29 + §16-M9 |

Offene v1-Posten bleiben erhalten: Unraid-Probelauf (Messwerte → M1-Planung
+ M9-README), Task-Chip `task_e5db0b72` (Kleinstposten, vor M1 erledigen),
Daten-Flaute-Check data.acoustid.org, XFF-Entscheid (spätestens M9).

---

## 5. Entscheidungsliste

Jeder Punkt mit Optionen und markierter Empfehlung; Konsens beider
Analysestränge bzw. beider Reviewer, wo vermerkt.

**E1 — Supervisor-Wahl (§5/§14.6).**
1. **supervisord + tini als PID 1 (Empfohlen)** — echte XML-RPC-API
   (Kriterium §5 wörtlich), `docker.py` ~1:1 übersetzbar,
   `stopsignal=INT`/`stopwaitsecs` je Prozess, in pytest ohne Container
   testbar; Konsens aus Code-Analyse und Recherche. Vorbehalt:
   Py-3.14-Verträglichkeit als erster M1b-Prüfpunkt (sonst eigener
   Interpreter im Image oder Neubewertung). Nur für **Dauerdienste**
   (PG/Index/API/Wächter) — Jobs siehe E10.
2. s6-overlay — beste Init-/Readiness-Maschine, aber Steuerung nur per
   CLI-Subprozess + Textparsing; CI braucht echte Container; execline.
3. Eigenbau im Wächter (asyncio.subprocess) — kein Drittwerkzeug, aber
   Reaping/Shutdown/Backoff selbst bauen.

**E2 — Index-Volume (K2).**
1. **Cache-Mount, konfigurierbar (Empfohlen)** — hält den
   messdatenbasierten v1-Entscheid (HDD kalt 40–80 s = Timeout); Kosten:
   ~70 GB Cache-Platz, v2-§3-Korrektur.
2. Array wie §3 wörtlich — jede erste Anfrage nach Schlaf läuft in 503.

**E3 — arm64 (K1).**
1. **amd64-only für M1–M8, arm64-Spike vor M9 (Empfohlen)** — §12
   relativieren; Spike baut fpindex für aarch64 (Zig) und fährt die
   198 Integrationstests.
2. arm64 sofort erzwingen — blockiert M1 auf unbestimmte Zeit.
3. arm64 ersatzlos streichen — verschenkt ARM-Hosts endgültig.

**E4 — Phasenfolge** wie §4 (M1a/M1b-Split, M2.5, Recherche-Gates,
M8 komplett).
1. **Übernehmen (Empfohlen)** — nur so bleibt die §16-Invariante „nach
   jeder Phase lauffähig" nachweisbar (E2E wandert mit M1b) und kein
   Datenmodell entsteht vor seiner §14-Recherche.
2. §16 wörtlich — Big-Bang-M1, UI-Phasen fallen durchs Raster.

**E5 — Release-Schnitt.**
1. **M1+M2 in einem Betreiber-Release (Empfohlen)**, plus Übergangslesen
   alter `AOFF_`-Variablen mit Deprecation-Warnung.
2. Zwei getrennte Breaking Releases mit zwei Migrationsanleitungen.

**E6 — Repo-Struktur (§11).**
1. **Bestehende Workspace-Struktur behalten, §11 als logisches Layout
   mappen (Empfohlen)** — neue Subsysteme als neue Workspace-Member
   (`mmo_discogs_dump`, `mmo_covers`, `mmo_tadb`, `mmo_mbref`); ein
   §11-wörtlicher Umbau wäre ein repoweiter Diff über ~980 Tests mitten
   in der riskantesten Phase, für Kosmetik.
2. §11 wörtlich als isolierte Codemod-Phase M2b nach M2.

**E7 — GPL-Compliance (K3).**
1. **NOTICES + Quell-/Commit-Pin + Quellangebot (Empfohlen)**, inkl.
   Fortschreibung des MIT-Lizenz-Entscheids (DECISIONS.md:152–159).
2. Index beim ersten Start herunterladen — bricht „ein Image" und
   Offline-Anspruch.

**E8 — DB-Schemata (§8).**
1. **Neue Migration `CREATE SCHEMA acoustid` + `SET SCHEMA` +
   `search_path` (Empfohlen)** — spezifikationstreu, Queries bleiben
   unangetastet, Katalog-Update auch bei 200–400 GB schnell.
2. Drei getrennte Datenbanken — kein Join `acoustid`↔`covers` für /v1.
3. Alles in `public` mit Präfixen — verletzt §8.

**E9 — Config-Migration.**
1. **AliasChoices (eine Release-Runde) + einmaliger Umschreiber beim
   Wächter-Start + Test mit einer v1-config.yaml (Empfohlen)** —
   verhindert die stille Amnesie (`submit.mode: off` → `local`).
2. Nur Umschreiber — ein fehlgeschlagener Start genügt für stille
   Defaults.

**E10 — Job-Ausführung (Importer/Discogs/Crawler/Backup/Queue-Send).**
Beide Reviewer belegen unabhängig: supervisord-`[program:*]` trägt ein
statisches `command=`, `startProcess()` kennt keine Argumente — die
Per-Lauf-Parameter des Importers (`--mode`, `--end-date`, `--report`, …)
und manuelle Jobs (§6.8) sind so nicht abbildbar; Eventlistener wären
zudem eigene Brückenprozesse.
1. **Jobs als direkte Subprozesse des Wächters
   (asyncio.create_subprocess_exec) (Empfohlen)** — Argumente,
   `returncode` und Report-Datei ohne Umweg (exakt der Vertrag aus
   `report.py`); §10.1 wird in DECISIONS auf **Dauerdienste**
   präzisiert (Jobs sind Kinder des Wächters, kein Docker-Zugriff —
   der Geist der Invariante bleibt).
2. supervisord-Jobs mit Job-Spez-Datei in `/config` (Wächter schreibt,
   Wrapper liest) — §10.1 wörtlich, aber zusätzlicher Mechanismus +
   Brückenprozess für den Ausgang.

**E11 — Plattenplatz-Guard (K6).**
1. **Ein Grenzwert `disk.min_free_gb`, aber Prüfung gegen jeden
   tatsächlichen Schreib-/Staging-Pfad (Empfohlen)** — mehrere Mounts
   sind mehrere Dateisysteme; `require_free_space` je Pfad existiert
   (`diskguard.py:47`), heute wird nur `dump_dir` geprüft.
2. §7 wörtlich (`update.min_free_gb`, ein Pfad) — Guard-Lücke bleibt.
3. Je Quelle ein eigener Grenzwert — teuerster Weg.

**E12 — Index-Prozess resident (K10 — bewusste Abweichung von v2 §1.2/§3;
dort gehört der Index zur Schlaf-Gruppe).**
Sachlage: Der Index-Start liest den kompletten Index per `MAP_POPULATE`
(Compose dokumentiert ~15 min bei 40–55 GB produktiv); der Weck-E2E-Wert
„1,3 s" stammt vom **leeren** Teststack und trägt keine
Produktionsaussage. `wake.hold_timeout_s: 90` ist mit einem
mitgestoppten Index voraussichtlich nicht haltbar — belastbar wird das
erst durch Messung (M1b/Probelauf: Index-Kaltstart auf SSD,
PG-Start/Stopp am echten Bestand).
1. **Index resident, Idle-Stopp betrifft nur PG + API (Empfohlen,
   unter Mess-Vorbehalt)** — auf dem Cache (E2) hält er das Array
   nicht wach; Kosten: RAM/Page-Cache dauerhaft belegt,
   v2-Korrektur nötig, Supervision des Index abweichend
   (autostart=true).
2. Index mit stoppen (spezifikationstreu) — nur haltbar, wenn die
   Messung den Kaltstart deutlich unter der Haltezeit zeigt; sonst
   503 bei jeder ersten Anfrage nach Schlaf.
3. Haltezeit auf Minuten erhöhen — erste Anfrage hängt minutenlang.

**E13 — Release-Compose.**
1. **Bind-Mounts (Empfohlen)** — `down -v` kann sie nicht löschen
   (die v1-`down -v`-Falle würde sonst auf 1–2 TB wachsen); passt zu
   Unraid-Shares. Named Volumes nur im Dev-/Test-Compose.
2. Named Volumes + README-Warnung.

**E14 — PG-Einbacken (K10 — bewusste Abweichung von v2 §12
„pg_upgrade-Schritt im Entrypoint").**
1. **Genau eine Major (18); Datenlayout `/data/db/<major>/`;
   Versions-Drift-Guard im Wächter (M1b: Startverweigerung +
   Log/Eventlog; Notification ab M2.5); Major-Upgrade als
   dokumentiertes Verfahren, `pg_upgrade`-Image-Variante erst bei
   realem Wechsel (Empfohlen).**
2. §12 wörtlich ab Tag 1 — verlangt zwei Major-Binärsätze im Image,
   bläht es dauerhaft.

**E15 — Supervision-Politik der Dauerdienste.**
1. **`autostart=false` + `autorestart=unexpected` mit begrenzten
   `startretries` für PG/Index/API (Empfohlen)** — supervisord startet
   per `stopProcess` Gestopptes **nicht** neu (kein Idle-Stopp-Loop),
   behält aber die Crash-Recovery, die v2 §3 dem Supervisor zuweist
   und die v1 mit `restart: unless-stopped` hatte. Regressionstest
   misst beides (Idle-Stopp bleibt gestoppt; Crash wird neu gestartet
   bzw. endet sichtbar in FATAL → `error`). Wächter selbst
   `autorestart=true`.
2. `autorestart=false` überall — kein Loop-Risiko, aber jeder
   PG-Absturz lässt den Stack bis zum nächsten Weckversuch tot liegen.

**E16 — Kleinere Festlegungen (im Paket empfohlen):**
`auth.allow_known_client_keys` und `mb.keep_submitted_mbid` behalten
(K7); `index.query_hashes` → `acoustid.index.query_hashes` in M2;
Importer-`REPORT_SCHEMA`-String stabil lassen; DB-Zugang intern künftig
vom Entrypoint generiert statt `.env`-Pflichtwert; alte GHCR-Pakete
stehen lassen („eingestellt" markieren); CI-Service-Container per
DECISIONS-Satz von der Ein-Container-Regel ausnehmen; Postgres
`stopsignal=INT` + großzügiges `stopwaitsecs` + `stop_grace_period`;
supervisord-Socket 0700, kein `inet_http_server`; Supervisor-/Kind-Logs
auf `/config` **mit Ausnahme des Wächter-Logs, das zusätzlich auf
stdout bleibt** — sonst wäre der dokumentierte Erstpasswort-Weg
(`docker logs`, `admin.py`, v2 §7 „geloggt") tot; `/status`-Feld
`stack` bleibt (nur additive Erweiterung).

---

## 6. Risiken (konsolidiert, nach Schwere)

| # | Risiko | Schwere | Gegenmaßnahme |
|---|---|---|---|
| R1 | `AOFF_DATA_DIR=/data`-Kollision: Wächter-SQLite/Keys/Cache lägen in v2 auf dem Array — Array schläft nie, kein Test merkt es | hoch | expliziter M1-Schritt `/config` + Test „data_dir nie unter Array-Mounts" |
| R2 | Falsche Supervision-Politik: Autostart macht Idle-Stopps rückgängig ODER fehlender Autorestart lässt Abstürze liegen | hoch | E15 (`autostart=false` + `autorestart=unexpected`, begrenzte Retries) + Regressionstests, die **beide** Richtungen messen |
| R3 | Volume-Migration verliert Bestände (PG-Layout `18/docker`; Index-On-Disk-Format des selbstgebauten Binaries) | hoch | Migrationsrezept schreiben und **auf Betreiber-Hardware proben**, bevor der Schnitt kommt |
| R4 | arm64-Zusage unerfüllbar (K1) | hoch | E3 |
| R5 | GPL-Pflichten beim GHCR-Push (K3) | hoch | E7 |
| R6 | Index auf Array (K2) macht das Weck-Erlebnis unbrauchbar | hoch | E2 + E12 (mit Mess-Gate) |
| R7 | Stille Config-Amnesie bei M2 (`submit.mode: off` → `local`) | hoch | E9 |
| R8 | Absturz maskiert sich als Schlaf (fehlende Kante `ready→error`, gewollter Zustand) | hoch | state.py-Erweiterung + „gestoppt ≠ abgestürzt"-Tests (M1b) |
| R9 | Portkollision 8080 (erst zur Laufzeit sichtbar) | mittel | `MMO_API_PORT` in M1a/M1b + Konfigurationstest |
| R10 | Postgres-Stoppsignal: TERM = Smart Shutdown hängt bis SIGKILL → Recovery bei jedem Wecken | mittel | `stopsignal=INT` + Nachweis-Test in M1b |
| R11 | E2E-Lücke: zwischen Docker-Ausbau und E2E-Portierung ist die Lauffähigkeits-Invariante nur behauptet | mittel | E2E-Portierung fest in M1b |
| R12 | `/_health` extern erreichbar bei breiter Proxy-Allowlist | mittel | explizite Deny-Regel + Test |
| R13 | Log-/Socket-Pfade aufs Array heben Invariante §10.2 lautlos auf | mittel | Pfad-Whitelist-Test (E16) |
| R14 | UID/GID-Gemenge (postgres, 6081, App-User) auf Array-Shares | mittel | UID-Matrix in M1 festlegen → README/Template (Unraid: nicht 99:100) |
| R15 | Weck-/Stopp-Kosten am echten Bestand unbekannt (E2E-Wert stammt vom leeren Teststack); Idle-Thrashing (Checkpoint/Recovery je Zyklus) | mittel | Messungen in M1b/Probelauf (PG-Start/Stopp, Index-Kaltstart) + Hysterese; LEARNINGS-Rubrik |
| R16 | Doppelarbeit, falls alte Phase 19 vor M1 gebaut würde | mittel | Phasenfolge E4 (Scheduler erst M2.5) |
| R17 | CI-Zeit/Image-Größe (PG+Index+Pillow); supervisord-Py-3.14-Frage | mittel | Multi-Stage, Layer-Cache, Größenbudget in M1 messen; Py-3.14-Check als erster M1b-Punkt |
| R18 | Testgekoppelte Doku (§5.1/§5.2) beim großen Umschreiben versehentlich editiert | mittel | Sperrzonen-Vermerk in jedem M-Auftrag |
| R19 | MB-Spiegel-Rechte/Verfügbarkeit für `cover_art_archive`/URL-Relationships ungeklärt (v2 §14.5) | mittel | Recherche-Gate + Grant-Prüfung vor M5/M7 |

---

## 7. Nicht-anfassen-Zonen (gilt für alle M-Aufträge)

- `ARCHITECTURE.md` §5.1 (Ströme-Tabelle) und §5.2 (DDL) — testgekoppelt;
  v2 §8 bestätigt Schema `acoustid` unverändert. Discogs/Covers werden
  **neue** Abschnitte, nie Edits an §5.2.
- Bug-für-Bug-Paritäten der API (docs/api-lookup.md, docs/api-submit.md).
- `compare2`/`extract_query`/Chromaprint nur mit Bit-Verifikation.
- Angewendete Migrations-SQL-Dateien (Drift-Sperre) — Änderungen nur als
  neue Migration.
- Fixtures nie committen; `pg_acoustid` nie ausliefern.

## 8. Nächste Schritte nach dem Betreiber-Go

1. M0-Doku-Sweep: `HANDOFF.md` → `docs/HANDOFF.md` (v1 →
   `docs/archive/HANDOFF-v1.md`); DECISIONS-Einträge — die 8 gekippten
   Entscheide aus der §16-Tabelle **plus** die zusätzlich betroffenen
   (MIT-Lizenzbegründung K3, Cache-Vollinvalidierung, Digest-Pin,
   pg_upgrade-Abweichung E14, Index-Residenz E12) sowie die Entscheide
   E1–E16; dafür eine vollständige DECISIONS-Durchsicht als
   Sweep-Bestandteil; PROGRESS-Statuskopf + neuer M-Phasenplan (alte
   Blöcke 19–29 ersetzt, Hinweise übernommen); ARCHITECTURE nur
   Kopfvermerk; LEARNINGS-Messwert-Rubriken. Commit + Diff-Anzeige.
2. Danach Pause + Go-Frage für M1a.
