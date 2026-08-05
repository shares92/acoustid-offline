# DECISIONS.md — acoustid-offline

Entscheidungslog. Neue Einträge oben anfügen. Format:
`## YYYY-MM-DD: Titel` / Entscheidung / Begründung / Alternativen.

---

## 2026-08-05: M1b-Entscheide — Ein-Container-Betrieb, Rechte, Secrets

Entscheidung (Phase M1b, Merge `6c4a184`; Details im PROGRESS-
Ergebniseintrag und den Quelldateien):
(a) **supervisord 4.3.0 läuft unter Python 3.14** (doppelt
verifiziert; PyPI-Metadaten „bis 3.13" sind konservativ) und liegt in
einem **eigenen venv** `/opt/supervisor` — Werkzeug des Images, keine
App-Abhängigkeit. (b) **Naht-Vertrag erweitert:** `all_running() ->
bool` wurde `inspect() -> GroupStatus` — supervisord unterscheidet
`STOPPED` (gewollt) von `EXITED/FATAL/BACKOFF` (Absturz), ein Bool
kann das nicht tragen; `sleeping` ist seither eine positive Aussage
(„alles Stoppbare ist STOPPED"), Teilzustände/`STARTING` sind nie
Schlaf. (c) **Gates:** hart nur für die DB (`pg_isready`, ohne
Treiber/Passwort — §8.2), und zwingende Gates gelten auch für schon
Laufendes (Review-Finding: `ALREADY_STARTED` hatte das Gate
übersprungen); Index/API weich, resident nur der Index (E12).
(d) **Prozess-Benutzer:** db=`postgres`, index=`acoustid` (6081, wie
v1), api=eigener User `api` (6082 — bewusst nicht `acoustid`, sonst
tauschte ein API-Exploit Socket-Zugriff gegen Schreibrecht am
Index); Wächter bleibt root (einziger Weg an den 0700-Supervisor-
Socket; Tradeoff in supervisord.conf dokumentiert).
(e) **`_FILE_MODE` der config.yaml 0600 → 0640 mit setgid-Gruppe
`musicmeta` auf `/config`** — überschreibt den Phase-3-Entscheid
„Secrets 0600"; die Schutzzusage bleibt („andere" lesen nie), ohne
die Änderung könnte kein unprivilegierter Dienst die Config lesen.
(f) **DB-Passwort:** kein `.env`-Pflichtwert mehr; der Entrypoint
erzeugt es (bzw. respektiert `AOFF_DB_PASSWORD_FILE`/Docker-Secret)
und exportiert nur den DATEIPFAD — nie den Klartext (drei Leak-Wege
im Review gefunden, inkl. psql-cmdline → stdin). App-Rolle ohne
Superuser: exakt `CREATEDB` + `pg_checkpoint`.
(g) **E2E fährt einen eigenen Compose-Projekt-Namespace** und die
Produktions-Compose hat keinen festen `container_name` mehr — ein
Test darf nie eine echte Instanz ersetzen können.
(h) `dump_dir` folgt `data_dir` NICHT mehr (GB-Dateien gehörten sonst
auf den Cache-Pool); SupervisorClient bleibt synchron (der Threadpool
sitzt eine Ebene höher, wie zuvor bei docker.py).
Begründung: HANDOFF v2 §3/§5, Entscheide E1/E10/E12/E14/E15/E16;
sechs Findings des blinden GPT-5.6-Zweitreviews, alle am Code
bestätigt — darunter zwei HOCH-Klassen (API-root; observe()
maskierte Teilzustände als Schlaf, ein Alt-Test hatte den Fehler
festgeschrieben).
Alternativen: api unter `acoustid` (Angriffsflächen-Tausch), 0600
mit root-API (keine Privilegientrennung), Passwort-Export in die
supervisord-Umgebung (erbt bis in den Fremdprozess fpindex),
`container_name` behalten (E2E-Kollisionsrisiko) — verworfen.

## 2026-08-04: M1a-Entscheide — Naht-Modul, Adress-Ableitung, Attrappen-Treue

Entscheidung (Phase M1a, Commits `51e3541`…`ff018d1`):
(a) Der Naht-Vertrag (`ProcessGroupController`-Protocol +
`ProcessControlError`) liegt in einem **eigenen Modul**
`watchdog/app/control.py` — nicht in `wake.py`: die Steuerungsmodule
müssen die Fehlerbasis importieren, `wake` importiert die Steuerung;
in `wake.py` wäre es ein Importzyklus. (b) `api_health_url` **folgt**
`api_base_url`, wenn ungesetzt (`_derive_defaults`, Muster wie
`config_path`/`dump_dir` unter `data_dir`); in Compose wird
`AOFF_API_HEALTH_URL` bewusst **ohne** Default durchgereicht
(`${AOFF_API_HEALTH_URL:-}`) — der Leerstring ist der Schalter für die
Ableitung. Diese Konvention übernimmt M1b für weitere abgeleitete
Werte. (c) `ReadinessProbe` verlangt die Adresse als Pflichtparameter
(bewusste Signaturänderung in der Naht-Phase): ein eingebauter Default
wäre ein zweiter Ort für die Umgebung und bliebe bei einer Umstellung
still falsch. (d) `FakeSupervisor` bildet die echte
supervisord-Semantik nach: Fault-Kette `BAD_NAME → ALREADY_STARTED →
SPAWN_ERROR(50)`, Absturz = `RUNNING→EXITED` (die Kante
`RUNNING→FATAL` existiert im Original nicht und ist über die
Attrappen-API nicht erreichbar), `FATAL` nur über
`start_failure()` (`STARTING→BACKOFF→FATAL`), PID nur für
STARTING/RUNNING/STOPPING.
Begründung: (a)–(c) Bau-Agent, im Review bestätigt; (d) Konsequenz aus
dem blinden GPT-5.6-Zweitreview — drei Treue-Fehler der Attrappe
(falscher Fault-Code, unmöglicher Zustandsübergang, PID bei BACKOFF)
hätten M1b-Tests auf Pfaden grün gemacht, die das echte supervisord
nie erzeugt.
Alternativen: Vertrag in `wake.py` (Zyklus), Health-URL mit
Compose-Default (halb umgezogene Umgebung bliebe lautlos), Attrappe
„einfach genug" lassen (M1b testete gegen Fiktion) — verworfen.

## 2026-08-04: M0-Betreiber-Entscheide E1–E16 zur v2-Migration

Entscheidung (alle vom Betreiber bestätigt; Herleitung, Optionen und
Belege: docs/research/m0-impact-analyse.md §5):
- **E1 Supervisor:** supervisord + `tini` als PID 1 — nur für die
  Dauerdienste (Postgres, Index, API, Wächter). Py-3.14-Verträglichkeit
  von supervisord ist der erste M1b-Prüfpunkt.
- **E2+E12 Index:** Volume auf dem SSD-Cache (v1-Messentscheid bleibt)
  UND Prozess bleibt beim Idle-Stopp resident — nur Postgres + API
  schlafen. Bewusste Abweichung von v2 §1.2/§3 (dort Schlaf-Gruppe),
  unter Mess-Vorbehalt (M1b: Index-Kaltstart auf SSD messen).
- **E3 arm64:** Release amd64-only für M1–M8; §12-Zusage relativiert;
  eigener aarch64-Spike (fpindex aus Quelle, 198 Integrationstests)
  vor M9 entscheidet über arm64.
- **E4 Phasenfolge:** M1a (Naht, weiter auf Docker) / M1b
  (Ein-Container inkl. E2E-Portierung) / M2 (Umbenennung) / **M2.5**
  (alte Phasen 19–22: Scheduler, Notify, Backup, Metrics) / M3–M7 mit
  **Recherche-Gates** (§14.2–14.4 je vor dem Datenmodell) / M8 =
  Admin-UI **komplett** / M9 Abschluss.
- **E5 Release-Schnitt:** M1+M2 als ein Betreiber-Release; alte
  `AOFF_`-Variablen eine Runde mit Deprecation-Warnung weiterlesen.
- **E6 Struktur:** uv-Workspace bleibt; v2-§11 wird logisch gemappt;
  neue Subsysteme als neue Member (`mmo_discogs_dump`, `mmo_covers`,
  `mmo_tadb`, `mmo_mbref`).
- **E7 GPL:** THIRD-PARTY-NOTICES + Quell-/Commit-Pin + Quellangebot
  für das eingebackene fpindex-Binary (GPL-3.0).
- **E8 DB:** neue Migration `CREATE SCHEMA acoustid` + `SET SCHEMA` +
  `search_path` je Verbindung; Alt-SQL bleibt unangetastet
  (Drift-Sperre).
- **E9 Config-Migration:** AliasChoices (eine Release-Runde) +
  einmaliger Umschreiber beim Wächter-Start + Test mit v1-config.yaml
  (gegen stille Amnesie: `submit.mode: off` → `local`).
- **E10 Jobs:** Importer/Discogs/Crawler/Backup/Queue-Send laufen als
  direkte Subprozesse des **Wächters** (Argumente, returncode, Report
  ohne Umweg). §8.1/§10.1 wird damit präzisiert: „Nur der Supervisor
  startet/stoppt **Dauerdienste**; Jobs sind Kinder des Wächters" —
  supervisord-`[program:*]` kann keine Per-Lauf-Argumente übergeben
  (beide Zweit-Reviews unabhängig).
- **E11 Plattenplatz-Guard:** ein Schlüssel `disk.min_free_gb`
  (ersetzt `update.min_free_gb`, Default 100), geprüft gegen **jeden**
  Schreib-/Staging-Pfad (mehrere Mounts = mehrere Dateisysteme).
- **E13 Release-Compose:** Bind-Mounts (Named Volumes nur Dev/Test) —
  die `down -v`-Falle würde sonst auf 1–2 TB wachsen.
- **E14 Postgres:** genau eine Major (18) im Image; Datenlayout
  `/data/db/<major>/`; Versions-Drift-Guard im Wächter (M1b:
  Startverweigerung + Log; Notification ab M2.5); Major-Upgrade als
  dokumentiertes Verfahren statt „pg_upgrade im Entrypoint" (bewusste
  Abweichung von v2 §12).
- **E15 Supervision-Politik:** `autostart=false` +
  `autorestart=unexpected` (begrenzte startretries) für PG/Index/API —
  per `stopProcess` Gestopptes wird nicht neu gestartet (kein
  Idle-Stopp-Loop), Abstürze schon; Wächter selbst `autorestart=true`;
  Regressionstests messen beide Richtungen. Postgres `stopsignal=INT`
  (Fast Shutdown) + großzügiges `stopwaitsecs` + Compose
  `stop_grace_period`.
- **E16 Kleinteile:** `auth.allow_known_client_keys` und
  `mb.keep_submitted_mbid` bleiben; `index.query_hashes` →
  `acoustid.index.query_hashes` (M2); Importer-`REPORT_SCHEMA`-String
  bleibt stabil; DB-Passwort intern künftig vom Entrypoint erzeugt;
  alte GHCR-Pakete bleiben stehen („eingestellt"); CI-Service-Container
  sind Testinfrastruktur und ausdrücklich von der Ein-Container-Regel
  ausgenommen; supervisord-Socket 0700, kein inet_http_server; Logs
  auf `/config`, **Ausnahme:** Wächter-Log zusätzlich auf stdout
  (Erstpasswort-Weg über `docker logs` bleibt); `/status`-Feld `stack`
  bleibt, Erweiterungen nur additiv.
Begründung: M0-Impact-Analyse (vier parallele Analysen, doppelt
reviewt: Opus 13 + GPT-5.6 12 Findings, alle verifiziert/eingearbeitet).
Alternativen: je Entscheid in der Analyse dokumentiert und verworfen.

## 2026-08-04: HANDOFF v2 „musicmeta-offline" übernommen — gekippte v1-Entscheide

Entscheidung: Das Projekt folgt ab sofort HANDOFF v2 (docs/HANDOFF.md,
03.08.2026; v1 archiviert als docs/archive/HANDOFF-v1.md). Damit sind
folgende v1-Entscheide **gekippt** (Alteinträge bleiben als Historie
stehen):
1. Stack aus 5 Containern / zwei Compose-Dateien → **ein** Container,
   **eine** Compose-Datei (Auftraggeber-Entscheidung v2 §3).
2. Getrennte Images Wächter/API/Importer → ein Image.
3. Wächter steuert Docker über /var/run/docker.sock (Eintrag
   2026-07-25) → interner Prozess-Supervisor; docker.sock entfällt
   ersatzlos.
4. Offizielle Postgres-/Index-Images, Index per Digest gepinnt
   (Eintrag 2026-07-25) → Postgres + fpindex werden ins eigene Image
   eingebacken; aus dem Digest-Pin wird ein Quell-/Commit-Pin.
5. Name acoustid-offline → musicmeta-offline (Scope-Erweiterung um
   Discogs, Cover Art Archive, TheAudioDB).
6. `update.time` → `acoustid.update.time` (+ neue Scheduler-Keys);
   `update.min_free_gb` Default 50 → 100 (als `disk.min_free_gb`,
   s. E11).
7. CAA außerhalb des Scopes → Voll-Spiegel + Lazy-Fallback.
8. Lookup-Cache-Invalidierung **vollständig** (Eintrag 2026-08-01) →
   ab M3 quellen-selektiv (v2 §10.6; TADB-Cache nur manuell).
9. MIT-Lizenzbegründung („GPL-Index läuft nur als separater
   HTTP-Dienst", Eintrag 2026-07-25) → fortgeschrieben: auch
   eingebacken bleiben es getrennte Prozesse mit HTTP-Schnittstelle
   (bloße Zusammenstellung), aber die **Weitergabe** des Binaries
   erzeugt GPL-Pflichten → E7.
Nicht gekippt (v2 §16 bestätigt): AcoustID-Fachlogik, Schema
`acoustid` (logisch — physisch erst mit E8 angelegt), Lookup-Cache-
Mechanik, Config-System, Auth/Keys, Rate-Limit, Bug-für-Bug-Paritäten.
Begründung: Betreiber-Handoff v2 vom 03.08.2026; Impact-Analyse
docs/research/m0-impact-analyse.md (dort auch die Korrekturen K1–K10
an v2 selbst, u. a. amd64-only-Index, Index-Volume, Backup-Mount).
Alternativen: Neuaufsetzen statt Migration — von v2 §16 ausdrücklich
ausgeschlossen.

## 2026-08-01: Stand-Vorprüfung vor jedem Bau-Agenten; Nachverifikation statt Doppelbau

Entscheidung: Jeder Bau-Auftrag an einen Agenten enthält künftig die
Vorprüfung „Ist die Phase laut PROGRESS-Statuskopf/git log bereits
umgesetzt? Wenn ja: nicht bauen, sondern prüfen und melden." Der
Orchestrator prüft vor dem Start zusätzlich selbst `git log` + Statuskopf
und vertraut weder Sitzungsgedächtnis noch Konversations-Zusammenfassung.
Begründung: Vorfall am 2026-08-01 — eine Session mit veraltetem Kontext
(Stand „Phase 6") erhielt das Go „weiter mit Phase 7", während das Repo
real auf Phase 13 stand; das Projekt läuft in mehreren Sessions parallel
weiter. Der Bau-Agent erkannte die Lage und lieferte statt eines
Doppelbaus eine vollständige Nachverifikation der bestehenden Phase 7
(1338 Tests grün gegen echte Container, Batch-Messreihe, manueller
Entescape-Durchstich bis in die DB) — dieses Verhalten ist jetzt der
vorgeschriebene Weg.
Alternativen: Blind neu bauen (Duplikat bzw. Überschreiben verifizierter
Arbeit) oder Auftrag kommentarlos abbrechen (verschenkt die günstige
Gelegenheit zur unabhängigen Prüfung) — beide verworfen.

## 2026-07-25: Korrektur — COPY-Escaping ist epochenabhängig; Parser-Regeln Phase 6

Entscheidung: Der Parser liest Delta-Dateien epochenabhängig: bis
einschließlich 2024-12-04 mit COPY-Text-Unescape (Backslashes
verdoppelt — betrifft praktisch nur `meta`), ab 2024-12-05 als reines
JSONL; je Zeile gibt es einen gezählten Fallback auf die andere
Lesart, Override per Parameter. Dies **korrigiert** den Eintrag
„Import-Verfahren" vom selben Tag: die dortige empirische Widerlegung
galt nur für die 2026er-Epoche (Stichprobe deckte die Zeitachse nicht
ab). Am Kernverfahren (zeilenweiser Parse + Batch-Upserts, kein
COPY-FROM-Staging) ändert sich nichts.
Weitere Phase-6-Entscheide: (a) **Lücken werden gemeldet, nicht
automatisch nachgeholt** — ein nachträglich eingespielter alter Tag
würde neuere Upsert-Stände überschreiben; (b) Zeitstempel/UUIDs
bleiben im Parser rohe Strings (Postgres castet; spart ein zweites
Parsen pro Zeile), Formprüfung per Regex; (c) Records als frozen
Dataclasses ohne Defaults (pydantic nur für Config — Heißpfad!); (d)
`verify_gzip` abschaltbar für den Bootstrap; (e) Downloader nutzt
`iter_raw()` statt `iter_bytes()` (Resume-korrekt).
Begründung: Empirisch belegt an Stichproben 2011–2026 und der neuen
Alt-Epochen-Fixture `2011-08-19-meta-update` (85/85 kaputte Zeilen
per Unescape wiederhergestellt; unabhängig von Agent und Fable
verifiziert). Ohne Epochen-Lesart wäre der Bootstrap an Tag 1,
Zeile 128 gescheitert — und scheinbar parsende Alt-Zeilen hätten
still falsche Werte geliefert.
Alternativen: Nur-JSONL-Lesart (bricht/verfälscht ~89 % der Historie),
automatisches Lücken-Backfill (Datenverfälschung), pydantic-Records
(Doppel-Validierung im Heißpfad) — verworfen.

## 2026-07-25: Index-Client — Bootstrap-Name, Healthcheck-Semantik, strikte Client-Validierung

Entscheidung: (a) Der Indexname kommt als Bootstrap-Variable
`AOFF_INDEX_NAME` (Default `main`) — der Container-Healthcheck braucht
ihn, und dort existiert keine config.yaml. (b) Der Compose-Healthcheck
prüft `/<name>/_health` (wget --spider; einziges HTTP-Tool im Image) —
der Dienst wird damit bewusst erst nach `ensure_index()` gesund:
Schreiber/Anleger (importer) hängen mit `condition: service_started`
ab, reine Leser (api) mit `service_healthy`. (c) Der Client validiert
`limit` (1–100), `timeout_ms` (1–10000), u32-Hashes und die
16-MiB-Grenze selbst, weil der Server Ausreißer teils still deckelt
statt abzulehnen.
Begründung: Empirische Befunde aus Phase 5 (siehe Addendum in
docs/research/phase1-acoustid-index.md); stilles Deckeln erzeugt
schwer diagnostizierbare Abweichungen.
Alternativen: Indexname in config.yaml (im Healthcheck nicht
verfügbar), Healthcheck nur auf `/_health` (prüft nichts), Serverwerte
ungeprüft durchreichen — verworfen.

## 2026-07-25: DB-Migrationen — eigener Runner, zwei Gruppen, lz4 in core

Entscheidung: Eigener schlanker Migrations-Runner in `shared/shared/db/`
(nummerierte SQL-Dateien als Package-Data, je Migration eine
Transaktion, `schema_migrations`-Protokoll mit Checksummen-Drift-
Erkennung, Advisory-Lock gegen parallele Starts, CLI
`python -m shared.db`) statt Alembic. Zwei Gruppen: `core` (Tabellen +
PKs) und `indexes` (Sekundärindizes) — der Bootstrap wendet erst
`core` an und zieht `indexes` nach dem Massenimport nach; global
aufsteigende Nummern über die Gruppen erzwingen identische Reihenfolge.
**`SET COMPRESSION lz4` liegt in `core`, nicht in `indexes`:** die
Einstellung wirkt nur auf neu geschriebene Werte — nach dem Bootstrap
gesetzt, bliebe genau der Erstbestand unkomprimiert (Fable-Entscheid
auf Agenten-Rückfrage).
Begründung: Raw-SQL-first-Design; Alembic brächte ORM-Kopplung ohne
Nutzen. Ein Test hält ARCHITECTURE-§5.2 und Migrations-SQL
anweisungsgleich — Doku und Schema können nicht divergieren.
Alternativen: Alembic (verworfen), lz4 in `indexes` (verworfen, s. o.),
FKs im Schema (bereits per §5.2 ausgeschlossen).
Nebenentscheide: Integrationstest-Schalter
`--integration=auto|require|off` (Abwahl immer sichtbar, `require`
scheitert laut); `tests/docker-compose.test.yml` publiziert 5432 nur
auf 127.0.0.1 für lokale Läufe — der Produktions-Compose bleibt bei
`expose`; `shared.db` bewusst nicht in `shared/__init__` re-exportiert
(psycopg lädt nur bei Bedarf; Wächter-Image bleibt schlank, optionales
Extra später möglich).

## 2026-07-25: Shared-Config — Designregeln (Phase 3)

Entscheidung (Paket zusammengehöriger Regeln):
- **Enum-Werte englisch** (`sleeping`, `forward_failed` …), da sie in
  YAML/JSON/SQLite/Postgres landen; die deutschen §9-Begriffe hängen
  als `display_name` an den Membern.
- **Leere Strings = „aus"** wird zentral über Properties abgebildet
  (`notify.enabled`, `backup.enabled`, `mb.configured`,
  `submit.upstream_enabled`); SMTP-Hauptschalter ist `host`
  (Port-Default 587, da ein Integer nicht „leer" sein kann); gesetzter
  Host verlangt from/to.
- **Fail fast:** `submit.mode: local+upstream` ohne
  `upstream_app_key` ist ein Validierungsfehler.
- **`mb.dsn` ohne Formatprüfung** (libpq akzeptiert URL- und
  Key-Value-Form); `notify.ntfy.url` muss http(s) sein.
- **Unbekannte Schlüssel:** Warnung mit vollem Pfad, dann ignorieren
  (upgrade-/downgrade-freundlich).
- **Secrets** als SecretStr, maskiert in repr/Dict; nur `save_config`
  schreibt Klartext — Datei mit Modus 0600.
- **`AOFF_LOG_LEVEL`** als zusätzliche Bootstrap-Env-Variable (das Log
  steht vor dem config.yaml-Read); kein pydantic-settings (11
  Variablen, eigene testbare from_env-Klasse).
Begründung: Konsistente, testbare Semantik an einer Stelle statt
verstreuter Konventionen; §6/§7-Anforderungen (Secrets nie im
Klartext, Modi-Schalter) direkt im Schema durchgesetzt.
Alternativen: deutsche Enum-Werte (bricht API-/DB-Konsistenz),
strikte Ablehnung unbekannter Schlüssel (bricht Upgrades),
pydantic-settings (unnötige Abhängigkeit) — verworfen.
Hinweis: config.yaml-Kommentare überleben ein Schreiben durch die
Admin-UI nicht (safe_dump); falls später nötig → ruamel.yaml.

## 2026-07-25: Python-Paketierung — Verzeichnisse nach §10, eigene Import-Namen

Entscheidung: Die Verzeichnisse bleiben exakt wie ARCHITECTURE §10
(`api/app`, `importer/app`, `watchdog/app`, `shared/`), installiert
werden die Pakete aber als `acoustid_api`, `acoustid_importer`,
`acoustid_watchdog` und `shared` (uv-Workspace, setuptools-Backend,
package-dir-Mapping; `shared/` mit einer Verschachtelungsebene
`shared/shared/`).
Begründung: Drei Pakete namens `app` würden sich in einem gemeinsamen
venv gegenseitig überschreiben; Workspace-Member-Wurzel und
Paketverzeichnis können nicht dieselbe Ebene sein. Hatchling scheidet
aus: Präfix-änderndes sources-Remapping bricht bei Editable-Installs
(empirisch verifiziert).
Alternativen: Verzeichnisse umbenennen (Abweichung von §10 sichtbar
statt intern), Hatchling (Editable-Bruch) — verworfen.
Folge: Neue Unterpakete müssen in `packages = [...]` der jeweiligen
pyproject.toml eingetragen werden (kein Auto-Discovery); Docker-Images
starten später z. B. `acoustid_api.main:app`.

## 2026-07-25: Code-Lizenz MIT

Entscheidung: Der eigene Code des öffentlichen Repos steht unter MIT
(Copyright „acoustid-offline contributors").
Begründung: Maximal einfach und kompatibel; üblich im AcoustID-Umfeld
(acoustid-server ist MIT). acoustid-index (GPL-3.0) läuft nur als
separater HTTP-Dienst und infiziert den eigenen Code nicht.
Alternativen: GPL-3.0, AGPL-3.0 — verworfen (Betreiber-Entscheid).

## 2026-07-25: Echte Dump-Fixtures nicht im öffentlichen Repo

Entscheidung: Die Test-Fixtures aus echten data.acoustid.org-Dateien
(tests/fixtures/acoustid-dumps/) werden nicht committet (.gitignore);
stattdessen liegt ein Fetch-Skript bei, das exakt dieselben 9 Dateien
reproduzierbar lädt (auch für CI nutzbar).
Begründung: Handoff-Scope „keine Weiterverteilung des Datenbestands";
ein öffentliches Repo mit echten Datenauszügen wäre genau das.
Nebenwirkung: Fixtures bleiben aktuell beschaffbar, solange die Quelle
die Historie vorhält (tut sie seit 2011 lückenlos).
Alternativen: Committen mit CC-BY-SA-Attribution (lizenzrechtlich
vertretbar, aber gegen den Handoff-Scope), synthetische Fixtures
(verlieren Beweiskraft gegen das echte Format) — verworfen.

## 2026-07-25: acoustid-index auf dem SSD-Cache-Pool

Entscheidung: Die Indexdaten (~40–55 GB erwartet, Volume ~70 GB) liegen
auf dem SSD-Cache-Pool (Unraid-Share „Prefer/Only: Cache"), nicht auf
dem Array (Betreiber-Entscheid auf Basis der Phase-1-Messdaten; löst
die vertagte Entscheidung vom selben Tag auf).
Begründung: Der Index lädt bei jedem Start seine kompletten Daten
(MAP_POPULATE) und braucht sie dauerhaft im Page-Cache; kalte Suchen
auf HDD dauern 40–80 s (= Timeout/HTTP 500), auf SSD ~1 s, warm ms.
Alternativen: Array (unbrauchbar laut Messdaten), erst Messlauf
(unnötig — die Größenordnung ist gesichert; Feinwerte liefert der
Phase-8-Probelauf).

## 2026-07-25: Rescoring per Python-Nachbau mit CI-Bit-Verifikation

Entscheidung: AcoustID-kompatible Scores entstehen zweistufig:
Index-Kandidaten (limit 20–40) → Rescoring in Python (Nachbau
`acoustid_compare2`, max_offset 80, Längenfilter ±7, Cutoff >0,4)
gegen den Vollvektor aus Postgres. Das offizielle Postgres-Image
bleibt (wie im Handoff). CI verifiziert `extract_query` und `compare2`
bit-genau gegen die Original-C-Extension, die dafür ausschließlich als
Test-Container läuft.
Begründung: pg_acoustid hat keine Lizenz (nicht weiterverbreiten) und
bräuchte ein Custom-PG-Image; ~50–150 ms Python-Rescoring je Lookup
sind für eine Privatinstanz unkritisch. Die offizielle
Python-Referenz von `extract_query` ist defekt — deshalb die
Bit-Verifikation gegen die autoritative C-Implementierung.
Alternativen: `ghcr.io/acoustid/postgres` als DB-Image (Abweichung vom
Handoff, Wartungsstand ungewiss) und eigenes PG-Image mit Extension
(Weiterverbreitung unlizenzierten Codes) — beide verworfen
(Betreiber-Entscheid).

## 2026-07-25: Query-Hash-Anzahl als Konfigurationswert

Entscheidung: Die Anzahl der indexierten Query-Hashes je Fingerprint
ist konfigurierbar (`index.query_hashes`, Default 120; z. B. 80 für
RAM-knappe Hosts). Der Phase-8-Probelauf liefert die Messwerte für
eine RAM-/Größen-Empfehlungstabelle in der Doku. Änderung des Werts
erfordert einen Index-Neuaufbau (dokumentieren).
Begründung: Betreiber-Vorgabe „erst messen und einstellen lassen, da
andere Systeme andere RAM-Größen haben" — das Projekt soll auf
beliebigen Docker-Hosts laufen, nicht nur auf dem Referenz-Server.
Alternativen: fester Wert 120 oder 80 — verworfen (Host-abhängig).

## 2026-07-25: Index-Image per Digest gepinnt; `ng` beobachten

Entscheidung: Es wird `ghcr.io/acoustid/acoustid-index` (Zig-`main`)
verwendet, per Image-Digest gepinnt. Der Nachfolger `ng` wird
beobachtet, nicht abgewartet.
Begründung: Kein Docker-Hub-Image, kein Release-Prozess, `main` ist
ein bewegliches Tag → Digest-Pin. `ng` hat kein Image und kein
Release-Datum, ist aber wire-kompatibel — unsere Integration überlebt
den Umstieg, nur der Index wäre neu zu befüllen.
Alternativen: `latest` (älterer Stand v25.4.0), `stable` (C++ 2022),
auf `ng` warten — verworfen.

## 2026-07-25: apikey-Modus — Whitelist-Schalter für Drittclient-Keys

Entscheidung: Der `apikey`-Modus akzeptiert die fest einkodierten,
öffentlich bekannten Keys von Drittclients (Picard `v8pQ6oyB`, beets
`1vOwZtEn`) nur, wenn `auth.allow_known_client_keys` aktiv ist —
**Default aus** (Betreiber-Entscheid).
Begründung: Eine ständige Whitelist würde bei Exponierung jedem
Zugriff geben, der die öffentlichen Keys kennt; im LAN-Normalfall
(auth `none`) sind Drittclients ohnehin nicht betroffen.
Alternativen: immer zulassen (schwächt Exponierungsschutz), nie
zulassen (sperrt Picard/beets im apikey-Modus komplett aus) —
verworfen.

## 2026-07-25: API-Kompatibilitätsvertrag nach Code-Recherche fixiert

Entscheidung: Die Kompatibilitäts-Anforderungen aus der
Quellcode-Recherche sind verbindlich (ARCHITECTURE §7 +
docs/research/phase1-api-formate.md): GET+POST, form-encoded +
gzip-Bodys, 1-MiB→413, Chromaprint-Base64-Decoder, meta-Präzedenz,
`sources` für Picard, Score-Semantik (>0,4/max 10/dedupliziert),
19 Original-Fehlercodes mit HTTP-Mapping, CORS. Korrektur zum
Handoff: `/v2/submission_status` statt `/v2/submit/status`.
Zusätzlich zum eigenen `/v2/lookup/batch` (100) wird das
Original-Batchprotokoll (`fingerprint.N` + `batch=1`, max 20)
unterstützt. Upstream: eigener Application-Key, `user`-Key
durchreichen, ≤3 req/s, eigenes Backoff, https.
Begründung: Reale Clients (Picard/beets) hängen an exakt diesen
Details (413-Batching, sources-Ranking, Base64-Variante).
Alternativen: nur Doku-Stand implementieren — verworfen (Doku ist
nachweislich unvollständig/teils falsch).

## 2026-07-25: MB-Query-Schicht — Raw-SQL, Online-Redirects, RO-Rolle

Entscheidung: Die MB-Anbindung folgt dem Phase-1-Entwurf
(docs/research/phase1-mb-schema.md): Raw-SQL statt mbdata-Paket; 10
Batch-Funktionen in genau einer Datei; Selfcheck + Schema-Guard beim
Start; Circuit-Breaker + eigene Exceptions für degradierten Betrieb;
Redirect-Auflösung für gemergte Recordings online bei Misses, Antwort
mit kanonischer MBID (Flag für Durchreichung); Read-only-Rolle
`acoustid_ro` per dokumentiertem SQL-Snippet (Betreiber führt es
einmalig aus); Dauer-Sekunden per Integer-Division abgeschnitten
(bit-kompatibel zum Original).
Begründung: Kapselung macht die jährliche MB-Schema-Änderung (bisher
nur additiv) zum Nicht-Ereignis; ohne Redirect-Auflösung lieferten
alternde track_mbid-Daten dauerhaft leere Metadaten; mbdata wäre eine
7000-Zeilen-Abhängigkeit mit eigener Schema-Kopplung.
Alternativen: mbdata-Modelle, FDW (bereits früher verworfen),
periodischer track_mbid-Rewrite statt Online-Auflösung (als spätere
Optimierung offen) — verworfen bzw. vertagt.

## 2026-07-25: Bootstrap per Voll-Replay aller Tagesdeltas, alle 7 Ströme

Entscheidung: Der Bootstrap spielt alle Tagesdeltas seit 2011-08-19 ab
(Stand heute: 5.454 Tage, 38.178 Dateien, 414 GB gz) — zur Laufzeit als
resumierbarer Importer-Job, niemals in Images gebündelt. Auf
Betreiber-Entscheid werden **alle 7 Ströme** geladen und importiert
(inkl. Usermeta `meta`/`track_meta`, ~19 GB extra; `track_puid` läuft
in der Lückenprüfung mit). Vor dem Vollimport ist ein zeitlich
begrenzter **Probelauf mit Messung** (Dauer, DB-/Index-Größe,
Hochrechnung) Pflicht — Phase 8.
Begründung: Es existiert kein Voll-Snapshot (`ExportTableFull`
unimplementiert; Alt-Dumps seit 2019 aufgegeben, 2021 angekündigte
Aggregate nie geliefert). Nirgends ist eine E2E-Importdauer belegt —
ohne Probelauf wäre der Vollimport ein Blindflug.
Alternativen: Nur Kern-Ströme (−19 GB) — vom Betreiber verworfen;
zeitlich beschnittener Korpus (z. B. letzte 5 Jahre) — verworfen
(unvollständiger Bestand = schlechtere Trefferquote).
Akzeptierte Lücke: Zeilen von vor 2011-08-19 ohne spätere Änderung
fehlen prinzipbedingt (Obergrenze ~10 % des Bestands).

## 2026-07-25: Import-Verfahren — direkter JSONL-Parse mit Batch-Upserts

Entscheidung: Zeilenweiser JSON-Parse + Batch-Upserts
(`ON CONFLICT (id) DO UPDATE`), Absent⇒NULL/false-Regel, `disabled`
explizit zurücksetzen; beim Bootstrap Sekundärindizes/FKs erst nach dem
Massenimport; Download und Import entkoppelt (Prefetch) mit Resume auf
beiden Ebenen (HTTP-Range; `import_state` je Strom+Tag). KEIN
COPY-FROM-Staging.
Begründung: Die Dateien sind valides JSONL — die COPY-Escaping-Hypothese
aus der Code-Analyse wurde an den Fixtures empirisch widerlegt (52
Quote-Werte parsen sauber); ein COPY-FROM-Textimport würde die Dateien
sogar korrumpieren. **[Korrigiert am selben Tag, Phase 6: gilt nur für
die Epoche ab 2024-12-05 — siehe Eintrag „COPY-Escaping ist
epochenabhängig".]** Bulk-Muster (Indizes nachziehen, Batches, Prefetch,
Resume) sind durch Prior Art belegt (chromaforge Apache-2.0, offizielle
populate-Skripte, dokumentierte CDN-Abbrüche).
Alternativen: COPY-Staging (verworfen, s. o.); Einzel-INSERTs
(verworfen, zu langsam für 100+ Mio. Zeilen).

## 2026-07-25: Fingerprint-Vektoren in Postgres, Index erhält nur Query-Extrakte

Entscheidung: Die vollen signed-int32-Vektoren liegen in
`fingerprint.fingerprint` (Postgres). Der acoustid-index erhält je
Fingerprint nur den extrahierten Query (Offset 80, max. 120 Hashes,
28-Bit-Maske, Silence-Hash gefiltert, unsigned) — als Python-Nachbau
von `acoustid_extract_query`. Die pg_acoustid-Extension wird nicht
eingesetzt; das Rescoring der Index-Kandidaten passiert außerhalb der
DB (Detail-Festlegung in Phase 1).
Begründung: Vollvektoren im Index bedeuten dokumentiert ~50 s statt
~50 ms pro Query (Aussage des AcoustID-Autors). pg_acoustid hat keine
Lizenzdatei und bräuchte ein Custom-Postgres-Image — das Handoff setzt
das offizielle Image. Der Extraktions-Algorithmus ist vollständig
bekannt und trivial nachbaubar.
Alternativen: pg_acoustid einsetzen (verworfen: Lizenz ungeklärt,
Custom-Image nötig); Vollvektoren in den Index (verworfen: Performance).
Hinweis: Präzisiert den Handoff-Wortlaut („Fingerprint-Vektoren leben im
acoustid-index") — der Index hält Extrakte, die Vollvektoren Postgres.

## 2026-07-25: Platzierung des acoustid-index vertagt bis nach Phase 1

Entscheidung: Ob der acoustid-index auf dem Array (Handoff-Annahme) oder
dem SSD-Cache-Pool liegt, wird erst nach den Phase-1-Kennzahlen der
aktuellen Index-Version entschieden (Betreiber-Entscheid auf Rückfrage).
Begründung: Alle Erfahrungswerte sprechen gegen HDD (Index muss
RAM-gecacht/SSD-nah sein; ~41–49 GB geschätzt) — aber die Zahlen stammen
von 2015 bzw. aus Fremdprojekten und gelten nicht verifiziert für die
aktuelle Zig-Implementierung.
Alternativen: Sofort Cache (Empfehlung der Recherche) oder sofort Array —
beide zurückgestellt bis zur Messung.

## 2026-07-25: Auth-Prüfung und Rate-Limit werden im Wächter durchgesetzt

Entscheidung: API-Key-Prüfung (`apikey`-Modus) und das IP-Rate-Limit
setzt der Wächter am Proxy durch — nicht der API-Service. Gilt auch für
Cache-Hits bei schlafendem Stack. (Rückfrage an den Auftraggeber,
entschieden 2026-07-25.)
Begründung: Die Key-Liste liegt in der Wächter-SQLite, und Cache-Hits
müssen geprüft werden können, ohne das Array zu wecken — das kann nur
der Wächter.
Alternativen: Prüfung im API-Service (bräuchte Key-Sync und ließe
Cache-Hits ungeprüft) oder doppelte Durchsetzung (Mehraufwand ohne
klaren Mehrwert im LAN) — beide verworfen.

## 2026-07-25: Steuerungsdateien kommen mit ins öffentliche Repo

Entscheidung: ARCHITECTURE.md, PROGRESS.md, DECISIONS.md und
LEARNINGS.md werden im öffentlichen GitHub-Repo geführt (ab Phase 2).
Begründung: Transparente, versionierte Projektsteuerung; die Dateien
enthalten keine Secrets.
Alternativen: Nur ARCHITECTURE.md öffentlich oder alle lokal —
verworfen (Betreiber-Entscheid auf Rückfrage).

## 2026-07-25: Admin-UI-Design bleibt vollständig bei der Design-Session

Entscheidung: Alles Designbezogene (visuelle Richtung, Navigation,
Chart-Lösung, Speicher-Interaktion, Badge-Ausgestaltung) wird
zurückgestellt, bis die separate Claude-Design-Session auf Basis von
docs/DESIGN_HANDOFF.md geliefert hat; die UI-Phasen 23–27 sind bis
dahin blockiert und nehmen keine Design-Entscheidungen vorweg.
Begründung: Design entsteht laut Handoff in der Design-Session; doppelte
oder vorweggenommene Entscheidungen würden Rework erzeugen.
Alternativen: UI parallel „nach Gefühl" bauen und später anpassen —
verworfen (Betreiber-Vorgabe 2026-07-25).

## 2026-07-25: Eigener schlanker API-Layer statt offiziellem acoustid-server

Entscheidung: `/v2/lookup`, `/v2/submit` und der Batch-Endpoint werden als
eigener FastAPI-Service implementiert; der offizielle acoustid-server wird
nicht deployt.
Begründung: Weniger Ballast; Dump-Import, MB-Direktanbindung und die
Modi-Schalter (auth/submit) wären beim offiziellen Server ohnehin
Sonderwege.
Alternativen: Offiziellen acoustid-server betreiben und anpassen —
verworfen wegen Anpassungsaufwand und unnötigem Funktionsumfang.

## 2026-07-25: acoustid-index als Matching-Kern

Entscheidung: Der Fingerprint-Suchindex ist das offizielle
acoustid-index-Image; die Fingerprint-Vektoren leben ausschließlich dort.
Begründung: Erprobter Suchkern; Eigenbau des Matchings wäre das größte
vermeidbare Risiko.
Alternativen: Eigene Matching-Implementierung (z. B. in Postgres) —
verworfen; acoustid-index bleibt gesetzt. Offene Detailfragen (Version,
API, Rebuild-Kosten) → Phase 1.

## 2026-07-25: MB-Metadaten per direktem Read-only-DB-Zugriff

Entscheidung: Der API-Service fragt die MusicBrainz-Postgres des
vorhandenen musicbrainz-docker-Stacks direkt read-only ab (gekapselte
Query-Schicht, `mb.dsn`).
Begründung: Entkoppelter und einfacher zu debuggen als eine
DB-zu-DB-Kopplung; bei MB-Ausfall degradierter Betrieb statt Fehler.
Alternativen: Foreign Data Wrapper in der AcoustID-Postgres — verworfen
(engere Kopplung, schwerer zu debuggen). Eigener MB-Spiegel im Projekt —
bewusst ausgeschlossen.

## 2026-07-25: Wächter steuert den Stack über /var/run/docker.sock

Entscheidung: Nur der Wächter startet/stoppt die Stack-Container, direkt
über den gemounteten docker.sock. API und Importer steuern nie Docker.
Begründung: Einfachste zuverlässige Weck-Mechanik auf einem
Docker-Host/Unraid; Risiko bewusst akzeptiert und mitigiert durch
minimalen Code im Wächter, Passwort-Login, Rate-Limit, LAN-Betrieb.
Alternativen: Docker-API über TCP/Socket-Proxy oder externe
Automatisierung — im Handoff nicht vorgesehen; Risiko-Abwägung ist
dokumentierter Teil der Architektur-Session.

## 2026-07-25: Getrennte Images für Wächter, API und Importer

Entscheidung: Drei eigene Images (watchdog, api, importer) plus
offizielle Images für Postgres und acoustid-index; Release immer mit
einem gemeinsamen Tag für alle drei aus einem Actions-Workflow.
Begründung: Minimale Angriffsfläche im docker.sock-Container,
Update-Entkopplung (nur Wächter-Neustarts sind spürbar), kleiner
Dauerläufer auf dem Cache; Versionskonsistenz über den gemeinsamen Tag.
Alternativen: Ein gemeinsames Image für alles — verworfen
(Angriffsfläche, Größe des Dauerläufers, Update-Kopplung).

## 2026-07-25: Config, Keys und Logs beim Wächter auf dem Cache

Entscheidung: `config.yaml`, API-Keys, Admin-Login, Update-Historie und
Event-Log leben beim Wächter (SQLite + YAML auf dem Cache-Pool), nicht
in der Array-Postgres.
Begründung: Die Admin-UI muss bei schlafendem Stack voll funktionsfähig
sein; kein UI-Aufruf darf das Array wecken.
Alternativen: Zentrale Ablage in der Stack-Postgres — verworfen (würde
das Array bei jedem UI-Zugriff wecken).

## 2026-07-25: Technologie-Stack Python/FastAPI/Jinja2+HTMX/Postgres/SQLite

Entscheidung: Eine Sprache (Python) für Wächter, API und Importer;
FastAPI als Web-Framework; Admin-UI server-rendered mit Jinja2 + HTMX
ohne Frontend-Build; PostgreSQL für den Datenbestand, SQLite für den
Wächter-Zustand; immer neueste stabile Versionen zum
Implementierungszeitpunkt.
Begründung: Ein Sprach-Ökosystem für alles reduziert Pflegeaufwand;
server-rendered UI vermeidet Build-Pipeline und npm-Abhängigkeiten im
Wächter-Container.
Alternativen: Im Handoff nicht weiter dokumentiert (Ergebnis der
Architektur-Session).

## 2026-07-25: On-Demand-Betrieb — nur der Wächter weckt

Entscheidung: Der Stack schläft im Normalzustand; der Wächter weckt bei
eingehender API-Anfrage (Anfrage wird bis `wake.hold_timeout_s` gehalten)
und beim täglichen Update; Auto-Stopp nach `idle.timeout_min`, nur wenn
keine Anfragen liefen und kein Import-/Backup-Job aktiv ist.
Begründung: Erfolgskriterium: Array-Platten dürfen herunterfahren; ein
einziger kleiner Dauerläufer auf dem Cache.
Alternativen: Dauerbetrieb des Stacks — verworfen (widerspricht dem
Projektziel schlafender Platten).

## 2026-07-25: Stale-Serving statt Wartungsfenster beim Import

Entscheidung: Während des Delta-Imports werden Lookups aus dem alten
Bestand weiterbedient; jede Delta-Datei ist eine eigene Transaktion;
der Import ist resumierbar über `import_state`.
Begründung: Kein Wartungsfenster nötig; robust gegen Abbrüche auf
langsamen Spindeln.
Alternativen: Import mit Schreibsperre/Downtime — verworfen.

## 2026-07-25: Lookup-Cache im Wächter mit vollständiger Invalidierung

Entscheidung: Ergebnis-Cache (Hash aus Fingerprint+Duration+
meta-Parametern → Antwort) auf SSD im Wächter; Cache-Hits wecken das
Array nicht; nach jedem erfolgreichen Delta-Import und jeder lokalen
Submission wird vollständig invalidiert.
Begründung: Wiederholte Lookups (häufig bei Tagging-Läufen) sollen das
Array gar nicht erst wecken; vollständige Invalidierung ist einfach und
garantiert Kohärenz.
Alternativen: Selektive Invalidierung — verworfen (Komplexität ohne
belegten Nutzen).

## 2026-07-25: Defaults — auth.mode `none`, submit.mode `local`

Entscheidung: API-Auth default `none` (Betrieb im LAN/VPN), Submit
default `local`; bei Exponierung nach außen zwingend `apikey` +
Reverse-Proxy mit TLS (Doku-Pflicht).
Begründung: Reibungsloser LAN-Betrieb als Normalfall; Schutzschalter
vorhanden und per Admin-UI umschaltbar.
Alternativen: Default `apikey` — nicht gewählt. Der Default `none`
(Handoff §11.6, zunächst Annahme) wurde vom Auftraggeber am 2026-07-25
explizit bestätigt.

## 2026-07-25: Backup nur für lokale Unikate

Entscheidung: Der zeitgesteuerte Backup-Job sichert ausschließlich
`local_submission`-Daten und die Wächter-SQLite in `backup.dir`.
Begründung: Der öffentliche Datenbestand ist jederzeit aus den Dumps
rekonstruierbar; nur Eigenes ist unwiederbringlich.
Alternativen: Vollbackup der Postgres — verworfen (dreistellige
GB-Größe ohne Mehrwert).

## 2026-07-25: Bewusste Ausschlüsse (Scope)

Entscheidung: Kein eigener MB-Spiegel, kein serverseitiges
Fingerprint-Berechnen, keine Metadaten-Suche/kein Browsing, keine
Mehrbenutzer-Verwaltung, kein Kubernetes/Helm, keine Weiterverteilung
des Datenbestands.
Begründung: Reine Fingerprint-Auflösung als Kernauftrag; alles Weitere
ist anderweitig vorhanden oder Lizenz-/Betreiberthema.
Alternativen: Jeweils Aufnahme in den Scope — verworfen (Handoff §5,
„Bewusst ausgeschlossen").

## 2026-07-25: Phase-7-Import-Details (Upsert-, Zeit- und Feed-Semantik)

Entscheidung: (1) `imported_at` wird auch bei Konflikt auf `now()`
gesetzt — es protokolliert wie `src_day` die *letzte* Anwendung.
(2) `src_day`/`imported_at` schreiben beide Fingerprint-Ströme; die
Disjunktheitsregel gilt nur für Dump-Spalten. (3) `created` bei
`fingerprint` per `COALESCE(bestehend, neu)` — keiner der beiden Ströme
überschreibt es. (4) `track_fingerprint.fingerprint_id != id` bricht
die Datei-Transaktion hart ab (Zusicherung aus §5.1; ein Bruch hieße:
Zuordnung an falscher Zeile). (5) `import_state.started_at` = `now()`,
`finished_at` = `clock_timestamp()`, sonst wäre die Importdauer
konstant null. (6) Index-Feed: erst `_update`, dann `indexed_at`;
Vektoren ohne indexierbare Hashes gelten als erledigt (sonst ewig im
Arbeitsvorrat), Zeilen ohne Vektor bleiben offen. (7) Feed-Batches per
Vorgabe mit `expected_version` abgesichert; ein zweiter Schreiber führt
zu lautem Abbruch. (8) `feed_index` ruft per Vorgabe `ensure_index()`
(der Importer legt den Index an, Compose-Healthcheck wird danach grün).
(9) Testinfrastruktur: conftest-Marker `db` zusätzlich zu `index`,
damit ein Test beide Dienste anfordern kann (rückwärtskompatibel).
Begründung: Konsistente Buchführungs-Semantik, laute statt stiller
Fehler bei Zusicherungsbrüchen, kein stiller Datenverlust im Feed.
Alternativen: Teil-Patches nur vorhandener Felder (verworfen —
Reaktivierungs-Falle §5.1); `indexed_at` vor dem `_update` (verworfen —
stiller Datenverlust); Erst-Import-Zeit in `imported_at` behalten
(verworfen — widerspräche `src_day`-Semantik „letzte Anwendung").

## 2026-07-25: Phase-8-Job-Details (Bulk-Sicherheit, Guard, Report)

Entscheidung: (1) Bulk-Modus = ausschließlich `synchronous_commit=off`,
als Sitzungseinstellung mit Rücknahme auf den *Vorher*-Wert (nicht
`RESET`); `fsync`/`full_page_writes` und jedes `ALTER SYSTEM`/`ALTER
DATABASE` sind tabu. Zusätzlich `maintenance_work_mem=1GB` nur für den
Indexbau. (2) `update.min_free_gb` wird als GiB gelesen (strengere
Lesart), `0` schaltet den Guard ab; gemessen wird das
Dump-Verzeichnis. (3) Der Index-Feed läuft im Bootstrap erst nach der
Gruppe `indexes`. (4) Eingespielte Tagesdateien werden gelöscht
(`--keep-dumps` behält sie) — 414 GB aufzuheben wäre teuer und
nutzlos. (5) `--end-date` benennt den letzten einzuschließenden Tag.
(6) Report per Default als JSON auf stdout, Datei atomar
(`.part`+Rename); 9 Exit-Codes bijektiv zu Ergebnissen (Test hält das
fest). (7) Zwei Compose-Variablen bewusst ohne `AOFF_`-Präfix
(`ACOUSTID_IMPORTER_IMAGE`, `ACOUSTID_WATCHDOG_DATA`), damit der
`AOFF_`-Satz deckungsgleich mit shared/env.py bleibt (Test vorhanden).
(8) importer/Dockerfile schon jetzt (Phase 29 übernimmt ihn für den
Release-Build); config.yaml wird read-only unter `/watchdog` gemountet.
Begründung: Die Sitzung ist das Sicherheitsnetz, das ein Prozesstod
nicht aushebeln kann; Korruptionsrisiken (fsync) sind mit Resume nicht
reparierbar und bleiben draußen; der Rest folgt „laut scheitern,
maschinenlesbar berichten".
Alternativen: `fsync=off` für mehr Durchsatz — verworfen (korruptes
Cluster statt verlorener Schwanz-Transaktionen); persistente
PG-Schalter — verworfen; Dumps behalten als Default — verworfen.

## 2026-07-25: Phase-9-Lookup-Details (Pipeline- und Formatentscheide)

Entscheidung: (1) Ergebnisliste wird auf 10 gekappt, DANACH je Track
dedupliziert — Original-Verhalten, auch wenn dadurch weniger als 10
Treffer übrig bleiben können. (2) Die Track-Auflösung folgt der
Merge-Verkettung über `track.new_id` (Tiefe ≤ 10), anders als das
Original — unser Bestand kommt aus den Deltas, dort bleibt
`fingerprint.track_id` am zurückgezogenen Track stehen. (3) Antwortet
der acoustid-index nicht, gibt es Fehler 13/HTTP 503 statt der stillen
leeren Trefferliste des Originals (kein erfundenes „kein Treffer").
(4) `compare2` und der Chromaprint-Codec liegen als pure
stdlib-Algorithmen in `shared/shared/fingerprint/`; `extract_query`
bleibt bei `shared.fpindex` (es definiert den Indexinhalt).
(5) gzip-Sonderfälle: kaputter gzip-Rumpf gilt als leerer Rumpf +
WARNING (die 19er-Tabelle kennt keinen Code dafür; das Original wirft
einen nackten 400); zu großes Content-Length bei gzip ⇒ 19/413.
`multipart/form-data` wird nicht gelesen (kein Lookup-Client nutzt es).
(6) Kandidatenlimit 40 (ARCHITECTURE erlaubt 20–40), `client` bleibt
Pflichtparameter wie im Original (nur Anwesenheit geprüft — Auth macht
der Wächter). (7) Testschalter `ACOUSTID_EXTENSION_DSN` ohne
`AOFF_`-Präfix (wie `ACOUSTID_INTEGRATION_TESTS`).
Begründung: Kompatibilität dort, wo Clients sie messen können
(Format, Reihenfolge, Limits); laute Fehler dort, wo das Original
Information verschluckt; Delta-Realität schlägt Original-Codepfad bei
der Merge-Verkettung.
Alternativen: Deduplizieren vor dem Kappen (verworfen — messbar anderes
Antwortverhalten als das Original); leere Liste bei Index-Ausfall
(verworfen — maskiert Betriebsfehler); compare2 im api-Paket
(verworfen — Domänenalgorithmus, nicht API-spezifisch).

## 2026-07-25: Phase-10-MB-Details (Query-Schicht, meta, Degradation)

Entscheidung: (1) Die MB-Schicht liegt in `shared/shared/mb/` (nicht
im api-Paket), weil der Wächter in Phase 25 den MB-Verbindungstest
braucht (`MbClient.check_connection()` liegt dafür bereit); Treiber ist
psycopg3 + psycopg_pool — die SQLAlchemy-Formulierungen des
Phase-1-Berichts beschreiben die Referenz, kein SQLAlchemy/mbdata im
Projekt. (2) Neuer Config-Schlüssel `mb.keep_submitted_mbid` (bool,
Default `false`): standardmäßig trägt die Antwort die **kanonische**
MBID aus der Redirect-Auflösung; `true` reicht die eingereichte durch.
(3) Fehlerbild ⇒ HTTP: `MbUnavailable` UND `MbSchemaMismatch`
degradieren zu 200 ohne Metadaten (§8.7); `MbQueryError` ⇒ 5/500.
SQLSTATE-Zuordnung: fehlende Tabelle/Spalte/Rechte/Schema ⇒ Mismatch
(Dauerzustand → degradieren), `statement_timeout` ⇒ Unavailable, Rest
⇒ QueryError. (4) Der Circuit-Breaker zählt Erreichbarkeit, nicht
Korrektheit (`MbQueryError` zählt nicht; Mismatch zählt); bei bekanntem
Selfcheck-Mismatch wird gar nicht erst abgefragt. (5) meta-Präzedenz
ist die Wahl des **Wurzelzweigs** (if/elif-Kette wie
`inject_metadata` im Original-Quelltext, an diesem belegt) — die
übrigen Schlüsselwörter wirken als Detail-Modifikatoren im gewählten
Zweig. (6) Metadaten werden einmal je Anfrage über eine gemeinsame
`track_id`-Zuordnung injiziert (Original-Verhalten); `recordingids`
nutzt die Index-Only-Existenzprüfung statt der Vollabfrage.
(7) Betriebswerte als dokumentierte Konstanten statt Config: Breaker
3 Fehler/30 s/30 s, Zeilenlimit 5000 + Truncation-Flag, connect 2 s,
statement 2000 ms, Pool max 4, Staleness 36 h/168 h, erwartete
Schema-Sequenz 31. (8) `sources` (track_mbid.submission_count) und
`usermeta` (meta/track_meta) kommen vollständig aus dem Delta-Bestand.
Begründung: ein Treiber im Projekt; §8.7 verlangt Degradation nur für
Nichterreichbarkeit — ein dauerhaft passendes Schema ist dem
gleichgestellt, echte Abfragefehler dürfen nicht leise verschwinden;
Kompatibilität dort, wo Clients sie messen (Präzedenz, compress-/
m2-Eigenheiten bug-für-bug, tabelliert in docs/api-lookup.md).
Alternativen: SQLAlchemy-Schicht (verworfen — neue Abhängigkeit ohne
Mehrwert); Schema-Mismatch als 500 (verworfen — degradierter Betrieb
ist das dokumentierte Verhalten bei kaputtem Spiegel); Config-Schlüssel
für Breaker/Timeouts (verworfen — ohne Messwerte vom echten Spiegel
wären es Scheinstellschrauben).

## 2026-07-26: Phase-11-Submit-Details (Ablage, Doc-ID-Raum, Modi)

Entscheidung: (1) Lokale Einreichungen leben **ausschließlich** in
`local_submission` (+ Suchindex), nie in den sieben Dump-Tabellen —
deren Delta-Upsert schreibt ganze Zeilen per expliziter ID und würde
lokale Einträge still überschreiben. (2) Auffindbarkeit über den
reservierten Doc-ID-Bereich `[2^31, 2^32-1]`: Doc-ID = 2^31 +
`local_track_id`; die Dokument-IDs des acoustid-index sind **u32**
(HOCH-Finding, empirisch gegen das gepinnte Image gemessen: 2^32-1
angenommen, ≥ 2^32 ⇒ HTTP 400 `IntegerOverflow` für den ganzen Batch;
der Client nahm zuvor unbelegt u64 an — korrigiert, Guard in
`fpindex/wire.py`). Disjunktheit typbedingt: `fingerprint.id` ist
Postgres-`integer` ≤ 2^31-1; Sequenz `AS integer … NO CYCLE` + Guard.
(3) Eine Zeile je eingereichter MBID (Original-Verhalten), Gruppierung
über `local_track_id`, ausgelieferte AcoustID = `local_track_gid`.
(4) `submit.mode off` ⇒ Fehler 12 „not allowed"/HTTP 400, geprüft VOR
dem Parsen. (5) **Synchron** indexieren im Request (`_update` → dann
Statuswechsel, Muster indexfeed), bewusst ohne `expected_version` (die
API ist nicht alleiniger Schreiber); kein Hintergrund-Worker — der
Stack dürfte sonst einschlafen, bevor die Einreichung im Index steht.
(6) Index nicht erreichbar ⇒ trotzdem HTTP 200 `"pending"`, Zeile
bleibt `new`, Nachtrag bei der nächsten Submit-Anfrage (max. 200) über
den Partialindex-Arbeitsvorrat. (7) `user` wird verlangt, aber nie
geprüft (kein Benutzerbestand; Auth macht der Wächter); `foreignid`
zählt bei der stillen Verwerfung als Zuordnung. (8) Keine
Dubletten-/Merge-Logik (zweimal eingereicht = zwei AcoustIDs) — die
Pflege-Warteschlangen des Originals sind bewusst außerhalb des Scopes.
Begründung: Datenhoheit des Importers ist Invariante (§5.2 Regel 2);
u32-Typbeleg macht den ID-Raum beweisbar kollisionsfrei; laute Fehler
nur, wo kein Datenverlust droht — ein Fehlercode bei Index-Ausfall
erzeugte Client-Retries und damit Dubletten.
Alternativen: Ablage in track/fingerprint mit reservierten IDs
(verworfen — Delta-Upsert überschreibt still); `off` als 13/503
(verworfen — Picard/beets wiederholen darauf, und 503 gehört dem
Wächter fürs Aufwecken); asynchroner Index-Worker (verworfen —
kollidiert mit dem Schlaf-Zyklus, Phase 16). Vormerkung Phase 19:
Submit↔Feed-Konflikt am `expected_version`-Guard (PROGRESS-Hinweis).

## 2026-07-26: Phase-12-Upstream-Details (Zeitpunkt, Bündelung, Queue)

Entscheidung: (1) **Weiterleitung zweistufig:** erster Versuch in der
Submit-Anfrage (`forward_after_submit`: nur die Gruppen dieser
Anfrage, max. 10, EIN HTTP-Versuch ohne Backoff, wirft nie),
Wiederholungen im Warteschlangenlauf (`drain_queue`: max. 500 Gruppen,
5 Versuche mit Backoff 1→2→4→8→16→30 s). §8.9 sagt „erneut versucht"
— der Erstversuch gehört in die Anfrage, sonst läge alles bis zum
Nachtlauf still (der Stack schläft dazwischen); Hintergrund-Worker
bleiben ausgeschlossen (Phase-11-Entscheid). (2) **Eine Anfrage je
Einreichungsgruppe** (`local_track_id`, alle MBIDs als mehrfaches
`mbid.0`) — Gruppen können verschiedene `user`-Keys tragen, und
Erfolg/Fehlschlag bleibt eindeutig der Gruppe zugeordnet.
(3) **`forward_attempts` zählt Läufe**, nicht HTTP-Versuche — sonst
wäre die 7-Grenze nach einem Drain-Lauf erreicht. (4) **Zwei
Fehlerklassen:** Transport (Netz/Timeout/408/429/5xx) ⇒ Lauf pausiert,
Zähler unberührt; inhaltlich (4xx/Fehlerpayload) ⇒ nur die Gruppe
scheitert. (5) **Drossel prozessweit** (Weiterleiter hängt am
ApiService): Schloss + monotone Uhr, harte ⅓-s-Treppe über Threads
hinweg. (6) **Upstream-Submission-IDs nur im Log-Ereignis**, nicht
persistiert — Phase 13 beantwortet ausschließlich lokale IDs; eine
Spalte wäre eine Migration ohne Abnehmer. (7) **Nur `indexed` wird
weitergeleitet** (eine Statusspalte; `new` darf „Index kennt sie
nicht" nicht verlieren); Fingerprint wird aus dem Vektor **neu
kodiert** (bit-verifizierter Encoder). (8) `submitted_by` leer ⇒
Fehlschlag ohne Anfrage (fremden Key raten wäre Zweckentfremdung);
Application-Key wird in jeder Fehlermeldung maskiert; nur https.
Begründung: Zweckbindung und Drosselvorgaben aus dem
Phase-1-Bericht sind Nutzungsregeln des fremden Dienstes —
Verstöße gefährden die Instanz (Key-Sperre); lokale Wahrheit
(gespeichert+indexiert) darf nie an Upstream-Fehlern hängen.
Alternativen: Weiterleitung nur im Nachtlauf (verworfen — Latenz ohne
Not); Bündelung über Gruppen hinweg (verworfen — user-Key-Konflikt,
unklare Fehlerzuordnung); Upstream-IDs in eigener Spalte (verworfen —
kein Abnehmer); HTTP-Versuche zählen (verworfen — 7-Grenze nach einem
Lauf erreicht). Vormerkung Phase 28: erster echter Lauf gegen
api.acoustid.org mit registriertem Key, mit einer Einreichung
beginnen.

## 2026-07-26: Phase-13-Batch/Status-Details (Vertrag des eigenen Endpoints)

Entscheidung: (1) **Batch-Rumpf ist eine Objekt-Hülle**
(`{"client", "meta", "maxdurationdiff", "queries": [...]}`), kein
nacktes Array — anfrageweite Felder bleiben möglich, ohne den Vertrag
später zu brechen; §7 wurde entsprechend präzisiert. (2) **Je Eintrag
eine vollständige AcoustID-Antwort** (`responses[]` mit `index`
0-basiert, `status` je Eintrag) — Clients werten Einträge mit
demselben Code aus wie Einzelantworten; Teilfehler bei **HTTP 200**.
(3) **Gemeinsame Betriebsmittel gehören der Anfrage:** Index weg ⇒
13/503 für alles (laute Absage aus Phase 9 bleibt), MB-Abfragefehler ⇒
5/500; nur eintragseigene Parameterfehler bleiben beim Eintrag.
(4) Grenze 100 ⇒ **19/413** (derselbe Code, auf den Picard sein
Batching stützt), geprüft vor dem Parsen; leeres `queries` ⇒ 200 mit
leerem Array. (5) **meta als Bündel je MetaPlan:** Einträge werden
nach ausgewertetem Plan gruppiert, `inject_metadata` läuft einmal je
Gruppe über alle Trefferobjekte — ein MB-Roundtrip-Bündel für 100
Einträge, Phase-10-Choreografie unangetastet. (6) `format` wird am
Batch nicht ausgewertet (immer JSON; XML hätte keinen sinnvollen
Elementnamen für die gemischte Liste); nur POST, GET ⇒ nacktes 405
(dokumentiert; einheitliches Fehlerbild wäre Proxy-Sache, Hinweis
Phase 15). (7) **Status-Mapping:** `new` ⇒ `"pending"`;
`indexed`/`forwarded`/`forward_failed` ⇒ `"imported"` mit `result.id`
= `local_track_gid` — `imported` heißt lokal „hat eine AcoustID und
ist nachschlagbar"; `forward_failed` bleibt `imported` (lokal fertig,
Upstream ist Betreibersache §8.9; `pending` ließe Clients ewig
fragen). (8) `id`-Obergrenze 100 ⇒ 19/413 (gezählt werden geschickte
Werte); unlesbare IDs still übersprungen, keine übrig ⇒ Fehler 2;
Antwort in Anfragereihenfolge je geschicktem Wert, DB einmal befragt;
antwortet auch bei `submit.mode = off` (reine Leseauskunft).
Begründung: Der eigene Endpoint hat kein Original-Vorbild — hier
zählt Konsistenz mit dem eigenen Lookup-Vertrag und Auswertbarkeit
durch bestehenden Client-Code; beim Status-Endpoint dagegen
Formatparität zum Original.
Alternativen: nacktes Array (verworfen — nicht erweiterbar);
HTTP-Fehlerstatus bei Teilfehlern (verworfen — Clients verwürfen die
ganze Antwort); Fehler je Eintrag auch bei Index-Ausfall (verworfen —
halb beantwortete Batches sähen aus wie „kein Treffer");
`forward_failed` als `pending` (verworfen — Client-Endlosschleife).

## 2026-08-01: API bekommt internen Healthcheck-Endpunkt — Bau in Phase 15

Entscheidung: Die API erhält einen **minimalen internen
Healthcheck-Endpunkt** als Bereitschaftsprüfung für das
Wake-on-request des Wächters (Vormerkung aus Phase 9). Gebaut wird er
**erst in Phase 15**, wo er erstmals gebraucht wird — Phase 14 bleibt
ein reines, paket-disjunktes Wächter-Paket. Ausgestaltung (nicht
öffentlich dokumentiert, prüft DB- und Index-Anbindung leichtgewichtig)
wird in Phase 15 festgelegt und dokumentiert.
Begründung: Bereitschaft über eine bestehende Route (z. B. definierte
Fehlerantwort von /v2/lookup) oder TCP-Connect zu erschließen ist
fragil gegenüber Verhaltensänderungen; §7 sieht zwar keinen Endpunkt
vor, ein interner, undokumentierter Prüfpfad bricht die Parität der
öffentlichen API aber nicht.
Alternativen: Bau schon in Phase 14 (verworfen — Paket nicht mehr
disjunkt, kein Abnehmer vor Phase 15); bestehende Route/TCP als Probe
(verworfen — fragil, verwechselt „Prozess lauscht" mit „Backends
bereit"). (Betreiber-Entscheid 2026-08-01.)

## 2026-08-01: Phase-14-Wächter-Details (Grundgerüst, SQLite & /status)

Entscheidung: (1) **Eigener SQLite-Migrationsläufer über `PRAGMA
user_version`** statt Mitnutzung von shared/db — eine Einzeldatei-DB
braucht weder Advisory-Locks noch Drift-Prüfung; eine Verbindung
hinter RLock, WAL, Zeitstempel ISO-8601-UTC wie im JSON-Log.
(2) **Ringpuffer exakt über die sortierte Auswahl** (`DELETE … WHERE id
NOT IN (SELECT id … ORDER BY id DESC LIMIT n)`), nicht über
`id <= MAX(id) - n` — AUTOINCREMENT-Lücken machten die Rechenvariante
zu großzügig; Schreiben+Beschneiden in einer Transaktion.
`EVENT_LOG_LIMIT = 5000` bleibt Konstante, kein §6-Schlüssel (§6 ist
eine abgestimmte Liste; Bedarfsfall Phase 27). (3) **Klartext-Passwort
nur ins Containerlog**, nie ins persistente event_log (läge dort hinter
genau der Anmeldung, für die es gilt); event_log erhält nur den
Vermerk. (4) **Datenstand für /status aus der eigenen
`update_run`-Kopie**, nie aus `import_state` (liegt auf dem Array —
Invariante §8.2 „weckt nie" ist damit baulich erfüllt); Phase 19 füllt
die Kopie aus dem Importer-Report. (5) **Reload-Signal als
Markierungsdatei** `config.yaml.reload` neben der config.yaml (JSON,
monoton wachsender Zähler, atomar via Temp-Datei+rename, 0644) — vor
Phase 15 existiert kein anderer Kanal; Empfangsseite im API-Dienst ist
Phase 15 (Vormerkung im Phasenblock). (6) **Stack-Zustand nur im
Speicher** — ein persistierter Wert wäre nach Neustart bestenfalls
veraltet; ab Phase 15 wird er aus Docker erhoben. (7) **Kein neues
AOFF_-Env für den SQLite-Pfad**: `watchdog.sqlite3` wird aus
`AOFF_DATA_DIR` abgeleitet (shared/env.py und .env.example sind
testgekoppelt und bleiben unberührt). (8) **docker-compose.watchdog.yml
trägt denselben Projektnamen** `acoustid-offline` wie die Stack-Datei,
damit beide das Volume `watchdog-data` teilen; `down -v`-Falle im
Dateikopf dokumentiert, auf Unraid entschärft ein Host-Bind-Mount
(`ACOUSTID_WATCHDOG_DATA`) das Risiko. (9) **Container-Healthcheck des
Wächters ist /status selbst** (python/urllib; Basisimage hat weder curl
noch wget) — prüft die Kette bis in die SQLite und weckt nichts.
Begründung: Alle Wege halten die Invariante §8.2 baulich ein (kein
Postgres-, Index-, MB- oder Docker-Pfad im Wächter der Phase 14) und
vermeiden Migrationen/Env-Erweiterungen ohne Abnehmer.
Alternativen: shared/db-Runner für SQLite (verworfen — falsche
Maschinerie); Limit als Config-Schlüssel (verworfen — §6 nicht
eigenmächtig erweitern); Passwort ins event_log (verworfen — Leck);
Datenstand aus import_state (verworfen — Array-Zugriff); eigener
Compose-Projektname + external Volume (verworfen — Wächter startete
erst nach dem Stack); Bind-Mount als Compose-Default (verworfen —
Musterbruch zu den übrigen Diensten).

## 2026-08-01: Phase-15-Details (Proxy, Docker-Steuerung, Wecken, Healthcheck, Reload)

Entscheidung: (1) **Docker-Steuerung ohne Fremdbibliothek** — die
Engine-API ist gewöhnliches HTTP, httpx spricht Unix-Sockets nativ
(`HTTPTransport(uds=…)`); genau drei Routen (inspect/start/stop),
Pfade **ohne** Versionspräfix (unversioniert = aktuelle
Daemon-Version; ein festes Präfix bräche gegen zu alte UND zu neue
Daemons). Die `docker`-Bibliothek brächte Images/Netze/Exec in den
Container mit der größten Angriffsfläche — „minimaler Code" ist die
Mitigation aus DECISIONS 2026-07-25. (2) **Keine neuen
AOFF_-Variablen**: Socket-Pfad, API-URL, Container-Namen sind
Modulkonstanten (Muster Phase 14, Punkt 7); Tests injizieren über
Konstruktorparameter. (3) **Healthcheck-Pfad `GET /_health`** —
Unterstrich-Präfix wie beim acoustid-index, außerhalb /v2/ kollisions-
frei; prüft DB (`SELECT 1`) und Index (`/<name>/_health`), bewusst
NICHT MusicBrainz (§8.7 — sonst stempelte ein MB-Ausfall den Stack als
nicht bereit); kein AcoustID-Fehlerformat, jeder Misserfolg → 503.
(4) **Reload-Teilmenge konservativ**: sofort übernommen werden
`submit.mode`, `submit.upstream_app_key` (inkl. Neubau des
UpstreamForwarder — sonst fehlte er nach dem Wechsel auf
local+upstream) und `mb.keep_submitted_mbid`; `index.query_hashes`
(Index-Neuaufbau, §6) und `mb.dsn` (Pool+Selfcheck nur beim Start)
werden auf den laufenden Wert zurückgeschrieben und als Warnung
geloggt — `service.config` beschreibt immer, was der Prozess wirklich
tut. Intervall 10 s. (5) **Ein Weckvorgang über asyncio.Task +
`wait_for(shield(task), timeout)`** — kein Lock, das im Fehlerfall
hinge; jede Anfrage bringt ihre eigene Haltezeit mit. Bei Weck-Timeout
bleibt der Zustand `starting` (der Stack startet vermutlich weiter);
`error` nur bei echtem Docker-Startfehler. `Retry-After` fest 30 s
(kein §6-Schlüssel; die Restdauer kennt niemand). (6) **Proxy reicht
alles roh durch** (Streaming beidseitig, roher Query-String,
Hop-by-Hop-Filter, rohe Antwort-Kopfzeilen) — auch das nackte 405 von
`GET /v2/lookup/batch` bleibt (Phase-13-Eigenheit; ein Proxy-eigenes
Fehlerbild wäre eine zweite Spezifikation). Eigene Antworten nur ohne
fremde Vorlage: 503+Retry-After im AcoustID-Format Code 13.
(7) **E2E-Wecktest als Marker `compose`** (opt-in wie `network`, läuft
nie in CI — `--integration=require` würde ihn auf jedem Runner mit
Docker-Socket erzwingen); CI-Struktur unverändert. (8) **api-Container
hat jetzt einen Compose-Healthcheck über `/_health`** (Spiegel von
Phase-14-Punkt 9). (9) **Bekannte, bewusst offene Lücken** →
Phase-16-Hinweis: von Hand gestoppter Stack (erste Anfrage 503 +
invalidate, erst die zweite weckt) und die geerbte Weck-Frist des
ersten Wartenden.
Begründung: Kleinste Angriffsfläche am docker.sock, Paritätstreue des
Proxys, degradierter Betrieb bleibt möglich, keine Env-/§6-
Erweiterungen ohne Abnehmer.
Alternativen: docker-py (verworfen — Funktionsumfang=Angriffsfläche);
versioniertes API-Präfix (verworfen — bricht beidseitig); MB im
Healthcheck (verworfen — §8.7); mb.dsn hot-reload (verworfen —
zweiter Verbindungslebenszyklus ohne Abnehmer); Retry-After aus
hold_timeout (verworfen — Scheingenauigkeit); Docker-Poller schon in
Phase 15 (verworfen — griffe der Phase-16-Zustandsmaschine vor).

## 2026-08-01: Phase-16-Details (Zustandsmaschine, Idle-Stopp, Poller)

Entscheidung: (1) **Übergangstabelle `ALLOWED_TRANSITIONS`** als
einzige Wahrheit über erlaubte Wechsel; jede Kante hat genau einen
Aufrufer. Bewusst verboten: `ready→error` (Fehler entsteht nur aus
gescheitertem Start/Stopp), `stopping→starting` (erst fällt der Stack
ganz), `error→stopping`, `sleeping→stopping`, `error→sleeping` (sonst
löschte der Poller den Fehler aus /status, bevor ihn jemand sieht).
(2) **Streng vs. nachsichtig:** Weck-/Stopp-Pfad nutzt `to()` (wirft —
dort wäre ein verbotener Wechsel ein Programmfehler), der Poller
`try_to()` (protokolliert, lässt stehen). (3) **Poller-Intervall 15 s,
Idle-Prüfung 30 s** — Modulkonstanten, keine §6-Schlüssel; Poller
zurückhaltend: nichts läuft ⇒ sleeping, alles läuft + Healthcheck ok ⇒
ready, alles dazwischen ⇒ Zustand bleibt stehen; bei laufendem Weck-/
Stoppvorgang (`busy`) wird übersprungen (kein Flackern); Docker weg ⇒
erste Warnung, danach Debug. (4) **Weck-Frist gehört dem Vorgang:**
jeder Dazukommende verlängert `_deadline` auf seine eigene Haltezeit —
kein Wartender sieht die 503 vor Ablauf seiner Zeit (schließt
Phase-15-Lücke 2). (5) **`stopping`:** Anfragen warten den Stopp ab
(shield auf die Stopp-Aufgabe) und wecken danach — ein halb
gestoppter Stack ist nicht bedienbar, und start/stop kämen sich ins
Gehege; niemand wird abgewiesen, solange die Haltezeit reicht.
(6) **Laufender Job sperrt nicht nur, er setzt die Leerlaufuhr
zurück** — sonst schliefe der Stack in der Sekunde ein, in der ein
langer Import endet. (7) **`JobSource`-Protokoll + `DatabaseJobs`**
über `update_run` (Lauf ohne Ergebnis läuft) — Phase 19/21 melden sich
mit `start_run` automatisch an. (8) **Doppelte Erzählung im
Ereignis-Log bleibt** (Vorgangs-Ereignisse Quelle `wake`,
Zustandswechsel Quelle `stack`) — die Logansicht filtert nach Quelle.
(9) **Kein Idle-Stopp aus `error`** — der Fehler bleibt sichtbar, bis
ein Weckversuch ihn auflöst (Phase 20 hängt die Notification an).
(10) **Kein zusätzliches Warten des Lifespans auf eine laufende
Stopp-Aufgabe** — `docker stop` ist idempotent, der Poller korrigiert
nach dem Neustart. (11) Bereitschafts-Flag statt `asyncio.Event`:
niemand wartet darauf (gewartet wird auf die Weck-Aufgabe), gesetzt
wird es auch aus dem Threadpool — `Event.set()` ist nicht threadsicher.
Begründung: Zustand bleibt einwertig und erklärbar; §8.5 baulich
erfüllt; Anzeige flackert nicht; Fehler bleiben diagnostizierbar.
Alternativen: Zustand persistieren (verworfen — nach Neustart
bestenfalls veraltet, Poller erhebt ohnehin); Poller-Intervall als
Config (verworfen — kein Betreiber-Nutzen); Stopp bei `error` nach
Frist (verworfen — verwischt Diagnose); Anfragen bei `stopping`
abweisen (verworfen — unnötige 503s).

## 2026-08-01: Phase-17-Details (Lookup-Cache)

Entscheidung: (1) **Eigene SQLite-Datei** `lookup-cache.sqlite3` (nicht
Dateicache, nicht die Zustands-DB — Phase-14-Entscheid): Buchhaltung
als `SUM(size_bytes)`, Verdrängung + vollständige Leerung je eine
Transaktion, `auto_vacuum=INCREMENTAL`. **Selbstheilend:** jeder
`sqlite3.Error` ⇒ Datei wegwerfen und neu anlegen; scheitert auch das,
schaltet sich der Cache still ab — ein Cache darf nie eine Anfrage
scheitern lassen. (2) **Schlüssel per Sperrliste** (SHA-256 über
[Schemaversion, Pfad, Parameterpaare], alles außer
`client`/`clientversion`): ein vergessener antwortprägender Parameter
wäre bei einer Erlaubnisliste eine FALSCHE Antwort, bei der Sperrliste
nur ein verpasster Treffer. Keine Umsortierung, keine
Case-Normalisierung, keine Default-Ergänzung; gzip-Formularrümpfe nur
für den Schlüssel entpackt (weitergereicht werden Originalbytes);
Methode nicht im Schlüssel (GET≡POST laut Vertrag). (3) **Eingelagert
nur HTTP 200 + JSON `status: "ok"`** — schließt Fehlerantworten und
`format=xml`/`jsonp` ohne zweiten Parser aus (kein realer Client nutzt
sie). **Batch nie gecacht** (identische Rümpfe wiederholen sich nicht;
Teilfehler stecken IN der 200er-Antwort und würden festgeschrieben).
(4) **Byte-Parität:** Kopfzeilen der API außer hop-by-hop/`date`/
`content-length` (Letztere aus dem bytegleichen Rumpf neu = gleicher
Wert); kein `X-Cache`. (5) **LRU über monotone `used_seq`-Spalte**
(ISO-Zeitstempel hätte Millisekunden-Kollisionen), geräumt bis 90 %
der Grenze; `cache.max_size_mb` als MiB, logisch verrechnet
(Rumpf+Kopfzeilen). Übergroße Einzelantworten werden nicht eingelagert.
(6) **Ein Cache-Hit zählt NICHT als Aktivität** — er braucht das Array
nicht; sonst hielte der Cache den Stack wach, den er überflüssig macht.
(7) **Invalidierung unabhängig von `cache.enabled`** (sonst überlebten
Alt-Einträge einen Aus/Ein-Zyklus über einen Import hinweg); einziger
Weg ist `service.invalidate_cache(reason)` mit Ereignis (Quelle
`cache`), Ereignis nur bei tatsächlich entfernten Einträgen. Auslöser:
erfolgreiche `POST /v2/submit`-Antwort (gemessen am HTTP-Status, Rumpf
bleibt streamend — zu oft leeren ist die harmlose Richtung),
`delta_import` (Phase 19), `manual` (Phase 25). (8) Anfragerumpf-
Pufferung für den Schlüssel auf 1 MiB gedeckelt; darüber läuft die
Anfrage ungecacht weiter (gelesener Anfang wird dem Strom wieder
vorangestellt). (9) `lookup-cache.sqlite3` gehört NICHT ins
Phase-21-Backup (Wegwerfware).
Begründung: Fail-safe-Richtung überall „verpasster Treffer statt
falscher Antwort"; §8.2 baulich (Cache-Zweig vor jedem Stack-Kontakt);
§8.6 vollständig.
Alternativen: Erlaubnisliste im Schlüssel (verworfen — falsche-Antwort-
Risiko); Batch je Eintrag cachen (verworfen — Proxy würde zweite
Vertragsquelle); Submit-Rumpf prüfen (verworfen — kostet Streaming);
Parameter sortieren (verworfen — Zusage ohne Not, Hitrate real
unbeeinträchtigt); Größe gegen page_count messen (verworfen — ohne
VACUUM nicht monoton).

## 2026-08-01: Phase-18-Details (Auth & Rate-Limit am Proxy)

Entscheidung: (1) **Reihenfolge Rate-Limit → Auth → Cache → Wecken** —
von außen nach innen, jeder Schritt teurer als der davor; Limit und
Auth laufen rein aus Wächter-Daten, §8.2 gilt damit auch für
abgewiesene Anfragen (Tripwire-getestet). `plan_request` liest den
Rumpf einmal VOR der Auth (client steht bei POST im Rumpf), geprüft
wird vor jedem Cache-Zugriff/Weckvorgang. (2) **Fehlercodes belegt**
(Phase-1-Fehlertabelle + api/app/errors.py): fehlender client 2/400,
ungültiger/inaktiver Key 4/400 (gesperrt = unbekannt, gleiche
Antwort), Rate-Limit 14/429, Rumpf > 1 MiB im apikey-Modus 19/413
(API antwortete ohnehin so — für eine feststehende Antwort wird nicht
geweckt). DB-Fehler bei der Key-Prüfung ⇒ nicht autorisiert, nie
stiller Durchlass. (3) **Ungesalzenes sha256 + compare_digest** für
Key-Hashes: hochentropische Maschinen-Keys, kein Wörterbuch zu
bremsen; argon2 je Anfrage (~50 ms, 64 MiB) wäre eine selbstgebaute
DoS-Fläche und ein Salt machte den UNIQUE-Index-Zugriff zum
Vollscan-verify. (4) **„Zuletzt benutzt" gedrosselt** auf 1
Schreibvorgang je 60 s und Key (Merker im Speicher, verliert ein
Neustart folgenlos) — Minutenauflösung genügt der Key-Liste der
Phase 26; Massenschreibvorgänge in der Zustands-DB bleiben tabu
(Phase-14-Linie). (5) **Exaktes gleitendes 60-s-Fenster je IP**
(Zeitstempel-Deque, ≤ Limit Einträge ≈ 4 KB/IP) statt Festfenster
(Burst-Verdopplung an der Grenze); Retry-After gerechnet; LRU-Deckel
2048 IPs + Minuten-Sweep; **abgelehnte Anfragen zählen nicht** (das
Limit soll bremsen, nicht dauerhaft aussperren); X-Forwarded-For wird
NICHT ausgewertet (fälschbar) — Vertrauensliste-Entscheid offen, Hinweis
Phase 29. (6) **503-Text generisch** = Original-Wortlaut zu Code 13
(Containernamen nur noch im Log/Ereignis); bewusste Abweichungen
dokumentiert: Retry-After bei 503/429 (Original schickt nie einen,
§7 verlangt ihn), eigene Fehlerantworten immer JSON auch bei
format=xml/jsonp. (7) **/status bleibt ohne Auth und ohne Limit**
(Bereitschaftsanzeige §7). (8) OPTIONS/Preflight wird im apikey-Modus
wie alles unter /v2/ geprüft (fail-closed; form-urlencodetes POST
löst kein Preflight aus). (9) Cache-Treffer zählen gegen das Limit
(Missbrauchsschutz für den Port, keine Kostenrechnung).
Begründung: fail-closed überall; Parität steigt sogar (503-Wortlaut);
keine §6-/Env-Erweiterung ohne Betreiber-Entscheid.
Alternativen: argon2 für API-Keys (verworfen — DoS-Fläche);
Festfenster-Limiter (verworfen — Burst-Verdopplung); X-Forwarded-For
immer auswerten (verworfen — fälschbar); OPTIONS ausnehmen (verworfen
— Schlupfloch ohne Bedarf); zuletzt-benutzt je Anfrage schreiben
(verworfen — Massenschreiblast).
