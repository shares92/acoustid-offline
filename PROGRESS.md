# PROGRESS.md — musicmeta-offline (vormals acoustid-offline)

Übergabe- und Steuerungsdatei: Statuskopf, Session-Übergabe, kompakte
Ergebnisliste der abgeschlossenen Phasen und der vollständige Plan der
offenen Phasen. Die ausführlichen Aufgabenblöcke erledigter Phasen
(inkl. der alten v1-Blöcke 19–29) liegen in der Git-Historie dieser
Datei und im Session-Archiv (`sessions/`). Quelle des Plans:
docs/HANDOFF.md (**v2**, „musicmeta-offline"; v1 archiviert unter
docs/archive/HANDOFF-v1.md) + docs/research/m0-impact-analyse.md;
technische Referenz: ARCHITECTURE.md (beschreibt bis M1/M2 den
gebauten v1-Stand, siehe Kopfvermerk dort).

**Status (2026-08-05, mittag): Phasen 0–18 (v1), M0, M1a, M1b und M2
abgeschlossen — das Projekt heißt überall musicmeta-offline und der
GitHub-Rename ist ausgeführt.** M2 (`1fc9f7f`, 14 Commits in zwei
sequenziellen Wellen + Kleinst-Posten): Env-Prefix `AOFF_`→`MMO_` und
Config-Keys aufs v2-Schema (E9: deklarative `LEGACY_KEYS`-Tabelle +
einmaliger Umschreiber beim Wächter-Start; Übergangslesen eine
Release-Runde — auch über die **Compose-Grenze**: beide Namenssätze
werden durchgereicht, `ports:` mit verschachtelter Interpolation,
per Layout-Tests eingeklagt), `/status` additiv um `components`
(PG-Major, Index-Commit), `release.yml` (v*-Tag → ein GHCR-Image,
`latest=auto`, Guard erzwingt die Rename-Reihenfolge), ARCHITECTURE/
README/Betriebs-Docs auf den gebauten v2-Stand (§5.1/§5.2
byte-identisch). Doppel-Review blind (Opus 7 + GPT 3 Findings,
Konsens-HOCH: Übergangslesen war auf dem Compose-Weg wirkungslos;
ein Reviewer-Fixvorschlag durch adversariale Verifikation als
schädlich widerlegt). Gates: Unit 1597, Integration 199 gegen das
eigene Image, E2E 8/8, CI 2× grün (4 Jobs). **GitHub-Rename
shares92/musicmeta-offline ausgeführt** (Redirects aktiv; Remotes
lokal + Tower nachgezogen). Erster Release-Tag bewusst offen bis
nach Messlauf-Auswertung + R3-Probe (Betreiber-Entscheid).
**Messlauf läuft auf Tower** (mittags bei 2011-09-20, ~15 min je
Delta-Tag der Anfangswelle, 0 Fehler). **Warten auf Go für M2.5.**

## Session-Übergabe (2026-08-05, M2 komplett)

**Kurzbeschreibung:** (1) **Messlauf-Kontrolle Tower:** läuft gesund
(Resume nach shfs-Absturz griff; mittags bei 2011-09-20, ~15 min je
Delta-Tag der ~4,4-GB-Anfangswelle, 0 Fehler, Dumps werden nach
Import gelöscht, Platz unkritisch: DB 36 GB/disk11 793 GB frei).
(2) **M2 in zwei sequenziellen Wellen** (Betreiber-Entscheid:
Kontextlast pro Agent begrenzen; parallele Zerlegung bei einem
Querschnitts-Rename bewusst verworfen — konfliktfreier Zuschnitt ≈ 1):
Welle 1 Code-Kern (Opus-Agent, 6 Commits), Welle 2 Doku-Umschrift
(frischer Opus-Agent, 6 Commits), dazu 2 Kleinst-Posten-Commits der
Koordination. (3) **Doppel-Review + adversariale Verifikation:**
Konsens-HOCH beider blinder Reviewer — das Übergangslesen war auf dem
Compose-Weg wirkungslos (nur `MMO_*` interpoliert; der dokumentierte
v1-Passwort-Übernahmeweg wäre ein No-Op gewesen, Zufallspasswort
statt Bestand). Fix: beide Namenssätze durchreichen, Warnung bleibt
beim Betreiber sichtbar; Layout-Tests klagen die Fehlerklasse ein.
Bonus-Fund: Healthcheck bei leerer `MMO_PORT` → dauerhaft unhealthy.
Ein GPT-Finding (`latest` durch Kurz-Tags wie `v2`) bestätigt und
verschärft; ein Opus-Finding (imagetools/Attestations) **widerlegt**
— der vorgeschlagene Fix hätte den Release-Job real gebrochen
(empirisch gegen echte Manifeste + buildx-Quelltext entschieden).
(4) **Gates doppelt:** Unit 1597 (eigener Lauf), Integration 199
gegen das eigene Image (70 s), E2E 8/8 (105 s), Image verifiziert
(PG 18.4, supervisord 4.3.0, Index-Commit-Label), CI 2× grün.
(5) **GitHub-Rename ausgeführt** (`gh api`), Redirect verifiziert,
`git remote set-url` lokal + Tower (Messlauf ungestört). GHCR: alte
v1-Pakete von Hand als „eingestellt" markieren (Token ohne
packages-Scope — UI-Schritt des Betreibers); neues Paket entsteht
erst beim ersten v*-Tag. (6) Zwei Code-Nachzügler inline gefixt:
Entrypoint-`chmod` toleriert read-only Docker-Secrets (EROFS brach
sonst den Start unter `set -eu`), zwei Docstrings (Legacy-Beispiel,
0640).

**Aktueller Stand — funktioniert (getestet, CI grün, unverändert):**
Alles aus dem v1-Stand bis Phase 18: Shared (Config/Env/Logging/
Modelle), DB-Migrationen, Index-Client, Importer komplett, API komplett
(/v2/lookup, /v2/submit off/local/local+upstream, Batch,
submission_status — bug-für-bug-dokumentiert), Wächter-Kern
(Grundgerüst/SQLite/Status, Proxy/Docker-Steuerung/Wecken,
Zustandsmaschine/Idle-Stopp, Lookup-Cache, Auth & Rate-Limit).
Ergebnistabelle unten.

**Existiert noch nicht:** Scheduler/Notify/Backup/Metrics (M2.5 —
die alten Phasen 19–22 wurden nie gebaut), Discogs/Cover/CAA/TADB/
v1-API (M3–M7), Admin-UI (M8), E2E-Suite in CI ausgeweitet/Release
(M9). Nie gelaufen: Voll-Bootstrap am echten Datenbestand, echter
Upstream-Submit, Volume-Migration v1→v2 auf echter Hardware, erster
v*-Release-Tag (bewusst zurückgestellt).

**Offene Punkte (priorisiert):**
1. **Go-Entscheidung M2.5** einholen (Scheduler/Notify/Backup/
   Metrics; Block unten). Nach M2.5: voller 5-Phasen-Doku-Sweep.
2. **Messlauf auswerten** (läuft auf Tower): Report
   `/mnt/cache/appdata/acoustid-offline/dumps/probelauf.json`, Log
   `/mnt/cache/appdata/acoustid-offline/messlauf.log`; danach
   query_hashes-Empfehlungstabelle + README-Zeitangabe +
   LEARNINGS-Messwerte (PG-Start/Stopp am echten Bestand,
   Index-Kaltstart auf SSD → entscheidet E12-Mess-Vorbehalt +
   startsecs-Tuning). Nichts wegräumen — der Stand ist der Anfang des
   echten Bootstraps.
3. **Volume-Migrationsrezept proben** (R3): docs/migration-v1-v2.md
   auf Tower am Probelauf-Bestand durchspielen, BEVOR der Bestand
   produktiv gebraucht wird (der neue Container-Entrypoint legt sonst
   ein leeres Cluster an — Abnahme §8 des Rezepts prüft darauf).
4. **Neue Betreiber-Entscheide aus den Recherche-Gates** (bei
   M3/M6-Go stellen): Discogs-Bilder-ToS vs. Lazy-Cache (Ⓞ8 in
   docs/research/m3-discogs-dumps.md, Empfehlung liegt bei); TADB-
   Key-Stufe (Empfehlung: Single Developer 8 €/Mon.); MB-GRANTs
   einspielen (Skript in docs/research/m5-mb-spiegel-befund.md §5)
   + Projekt-Container ins MB-Docker-Netz.
5. **Daten-Flaute:** entschärft — Deltas bis 2026-07-27 (geprüft
   04.08.); Export hinkt ~8 Tage nach. Vor Produktivstart erneut
   prüfen.
6. Log-Rotation für `/config/logs/watchdog.log` (tee-Datei) → M2.5.
7. Echter Upstream-Lauf + Drittclient-Tests: bewusst erst M9.
8. **XFF-Betreiber-Entscheid**: spätestens M9.
9. **Erster Release-Tag** (`v*` → GHCR-Image): erst nach
   Messlauf-Auswertung + R3-Probe (Betreiber 2026-08-05). Danach
   GHCR-Paket von Hand öffentlich stellen; alte
   `acoustid-offline-*`-Pakete als „eingestellt" markieren
   (UI-Schritt, Token ohne packages-Scope).

**Nächster konkreter Schritt für eine frische Session:** `git log
--oneline -5` + diesen Statuskopf lesen (Pflicht-Vorprüfung), dann
Messlauf-Stand auf Tower prüfen (Pfade oben; `ssh Tower`,
Betreiber-Freigabe 2026-08-04) — ist er fertig: Auswertung (Punkt 2)
und R3-Probe (Punkt 3) vor M2.5 anbieten. Per AskUserQuestion das Go
für **M2.5** einholen; bei Go einen Opus-Bau-Agenten mit dem
M2.5-Block unten beauftragen — inklusive Stand-Vorprüfung und
Sperrzonen-Vermerk. Achtung: Repo-Hooks verlangen `pytest`-Aufrufe
ohne Pipe (`cmd > log; echo rc=$?`) und stellen bei
`--compose/--network` Rückfragen — E2E-Läufe deshalb vom
Orchestrator fahren. Serena-Symboltools NIE in Worktrees verwenden
(schreiben ins Haupt-Repo — LEARNINGS).

**Fallstricke — nicht ändern / beachten:**
- ARCHITECTURE-§5.2-DDL und §5.1-Ströme-Tabelle sind **testgekoppelt**
  — nie freihändig editieren; Discogs/Covers werden NEUE Abschnitte.
- Bug-für-Bug-Paritäten der API sind Absicht (Abweichungstabellen in
  docs/api-lookup.md / docs/api-submit.md) — „Original-Bugs" nicht
  fixen.
- `compare2`/`extract_query`/Chromaprint-Encoder nur mit
  Bit-Verifikation (CI-Job `extension`) ändern.
- Angewendete Migrations-SQL-Dateien nie editieren (Drift-Sperre) —
  Schema-Änderungen nur als neue Migration (betrifft E8).
- Doc-ID-Bereich für Submissions ist [2^31, 2^32-1] (u32-Grenze real).
- Fixtures (`tests/fixtures/acoustid-dumps/*.jsonl.gz`) nie committen;
  Beschaffung über fetch_fixtures.py. `pg_acoustid` nie ausliefern.
- Skripte/Container nie mit CWD=Repo-Root starten (Namespace-Falle);
  psycopg in shared bewusst lazy.
- **Compose-Tests haben Netz.** Der Marker `network` wählt nur Tests ab;
  der Container hängt am genatteten Default-Bridge-Netz. Wer in einem
  E2E-Test „ohne Netz" braucht, klemmt die Quelle selbst ab (`/etc/hosts`
  → 127.0.0.1) — sonst zieht ein Delta-Lauf echte Dumps von
  data.acoustid.org (Fair-Use, §12 Punkt 9). Dazu: ein unbegrenzter
  `update`-Lauf auf leerer `import_state` ist gesperrt (Kaltstart-Sperre,
  DECISIONS 2026-08-05).
- Lokale Integrationstests auf Apple Silicon: colima mit
  `--vz-rosetta`; CI-Flakes durch Docker-Hub-Timeouts → `gh run rerun
  --failed`.
- **Neu (v2):** fpindex ist GPL-3.0 → Einbacken nur mit NOTICES/
  Quellangebot (E7). `/data` ist in v2 das **Array** — Wächter-Daten
  gehören nach `/config` (R1). acoustid-index ist amd64-only (E3).
  `down -v` löscht künftig bei Named Volumes bis zu 2 TB → Release-
  Compose nur mit Bind-Mounts (E13). PG-v1-Volume mountet
  `/var/lib/postgresql` mit Daten unter `18/docker` — Volume-Migration
  nur nach dokumentiertem, geprobtem Rezept (R3).

## Arbeitsregeln

- Session-Start: zuerst ARCHITECTURE.md und PROGRESS.md lesen, dann
  fragen, welche Phase dran ist. Vor jedem Bau-Agenten-Start `git log`
  + Statuskopf prüfen; Bau-Aufträge enthalten die Vorprüfung „Phase
  bereits umgesetzt? → nicht bauen, sondern prüfen und melden"
  (DECISIONS 2026-08-01).
- Implementierung einer Phase erst nach explizitem Go des Auftraggebers.
- Keine Annahmen außerhalb des Handoffs (jetzt: v2 + Impact-Analyse) —
  bei Unklarheit nachfragen (Optionen + Empfehlung).
- **Jeder M-Auftrag trägt den Sperrzonen-Vermerk** (§7 der
  Impact-Analyse: §5.1/§5.2, API-Paritäten, Bit-Verifikation,
  Migrations-Drift-Sperre, Fixtures/pg_acoustid).
- M3/M4/M6 beginnen mit ihrem **Recherche-Gate** (§14 v2); Ergebnisse
  als Bericht nach docs/research/, erst dann Datenmodell/Bau.
- Nach jeder abgeschlossenen Phase: Statusvermerk hier, Doku aktuell
  halten, bevor die nächste Phase beginnt; danach Pause + Go-Frage.
- Nach Abschluss jeder 5. Phase: voller Doku-Sweep (PROGRESS,
  DECISIONS, ggf. ARCHITECTURE/LEARNINGS) mit Diff-Anzeige.
  (Zählung neu ab M0; nächster voller Sweep nach M2.5.)
- Jede UI-Phase endet mit Sicht-Check am gerenderten Ergebnis.
- Admin-UI (M8): Referenz ist das Designpaket unter docs/design/ bzw.
  dessen v2-Fassung (DESIGN_HANDOFF v2, separate Design-Session).
- Messwerte aus M1b/Probelauf/Importläufen in die LEARNINGS-Rubriken
  eintragen (v2 §16).

---

## Ergebnisse der abgeschlossenen Phasen 0–18 (v1) und M0

Vollständige Aufgabenblöcke: Git-Historie dieser Datei; Entscheide:
DECISIONS.md; Berichte: docs/research/.

| Phase | Titel | Commit(s) | Kernergebnis |
|---|---|---|---|
| 0 | Recherche Dump-Format & Bootstrap | — | Kein Voll-Snapshot: Replay aller Tagesdeltas seit 2011-08-19 (414 GB gz, 38.178 Dateien); exakte Schemata (§5.1/§5.2); Fixtures |
| 1 | Recherche Index/Upstream/MB | — | acoustid-index ausgeleuchtet (Cache-Pool-Entscheid), API-Vertrag inkl. `/v2/submission_status`-Korrektur, MB-Query-Schicht-Entwurf (docs/research/) |
| 2 | Repo-Grundgerüst & CI | 15c78e4, 6fde05c | uv-Workspace Python 3.14, öffentliches Repo, CI (setup-uv-Tag-Fix), MIT, fetch_fixtures.py |
| 3 | Shared: Config/Modelle/Logging | 481315c | Alle §6-Schlüssel (pydantic, Secrets 0600, YAML-Sexagesimal-Fix), EnvSettings, JSON-Logging |
| 4 | DB-Schema & Migrationen | 1f294c4 | Eigener Runner, Gruppen core/indexes, lz4 in core, postgres:18; DDL testgekoppelt an §5.2 |
| 5 | Index-Client | 620aa5a | msgpack-Client (query/wire/client/errors), Digest-Pin, AOFF_INDEX_NAME, 12-Befunde-Addendum |
| 6 | Importer: Download & Parser | 0893abe | Epochen-Lesart COPY-Escaping (≤2024-12-04, HOCH), Range-Resume (iter_raw), Arbeitsliste mit Lücken-Meldung |
| 7 | Importer: DB-Import & Index-Feed | 85d7d40 | Datei=Transaktion inkl. import_state, Resume, Feed 1000er nach id; nachverifiziert 2026-08-01 (1338 Tests) |
| 8 | Bootstrap, Guard & One-Shot-Job | c915fb8 | Bulk-Pfad (core→Import→indexes→Feed), 9 Exit-Codes, Report-Schema, Probelauf-Modus; **DoD-Rest: Unraid-Lauf offen** |
| 9 | API: /v2/lookup Kern | 027597c | Match-Pipeline mit compare2-Nachbau, Bit-Verifikations-CI (fand Phase-5-Fehler), docs/api-lookup.md |
| 10 | API: MB-Resolver & meta | a467064, ea008a6 | shared/mb (Circuit-Breaker, Selfcheck, Staleness), meta bug-für-bug, Online-Redirects, degradierter Betrieb |
| 11 | API: /v2/submit off/local | ead4790, b15c60b | local_submission, synchrone Indexierung, Doc-ID u32-Finding [2^31, 2^32-1], docs/api-submit.md |
| 12 | API: Upstream & Queue | 657ee14 | drain_queue/retry_forward, ≤3 req/s, 7-Fehler-Grenze → upstream_forward_gave_up, Mock-Upstream-Tests |
| 13 | API: Batch & submission_status | 1d8874a | queries/responses-Vertrag (Teilfehler bei 200, Limit 100→19/413, meta-Bündelung); Status nie 404 |
| 14 | Wächter: Grundgerüst, SQLite & /status | 7ce2cd5 | Paket `acoustid_watchdog` (FastAPI), SQLite-Migrationsläufer, event_log-Ringpuffer 5000, /status baulich weckfrei, Erststart-Passwort argon2 nur Containerlog, Reload-Marke, compose+Healthcheck |
| 15 | Wächter: Proxy, Docker-Steuerung & Wecken | 3f9daee | Reverse-Proxy `/v2/*` roh/streamend, DockerClient (uds), WakeCoordinator (Task+shield; Timeout → 503+Retry-After 30), `GET /_health`, Reload-Empfang; E2E: Weckdauer ~1,3 s lokal (leerer Teststack) |
| 16 | Wächter: Zustandsmaschine, Idle-Stopp & Startfehler | a42c192 | `ALLOWED_TRANSITIONS` (25 Paare), IdleStopper (Job-Blockade §8.5 via `JobSource`), StatePoller 15 s, Weck-Frist gehört dem Vorgang; E2E 4/4 |
| 17 | Wächter: Lookup-Cache | 8c3816c | Eigene SQLite selbstheilend, Schlüssel SHA-256 per Sperrliste, nur 200+`status: ok`, Byte-Parität, LRU über Sequenz, Hit ≠ Aktivität, `invalidate_cache(reason)`; E2E 5/5 |
| 18 | Wächter: Auth & Rate-Limit | 2570ea5 | Reihenfolge Limit → Auth → Cache → Wecken (Abweisungen wecken nie), `apikey` gegen `api_key` (sha256 konstant-zeitig), Whitelist Picard/beets, Codes 2/4/14/19, 60-s-Gleitfenster je IP (LRU 2048); E2E 6/6 |
| **M0** | **Impact-Analyse HANDOFF v2** | 5a38d2f | docs/research/m0-impact-analyse.md: Betroffenheit (~10 % Wächter-Code, 0 Zeilen API/Importer/Shared-Steuerung, ~26 % Wächter-Testzeilen, E2E/Repo-Layout-Tests), Korrekturen K1–K10 an v2, Phasenplan M1a–M9, Entscheide E1–E16 (Betreiber bestätigt); Doppel-Review Opus+GPT-5.6, alle 25 Findings verifiziert/eingearbeitet |
| **M1a** | **Naht-Phase (weiter auf Docker)** | 51e3541…ff018d1 | `control.py` (ProcessGroupController-Protocol + ProcessControlError), Adressen/`api_port` in EnvSettings (Defaults exakt erhalten; Compose reicht `AOFF_API_*` durch, Leerstring = Ableitungs-Schalter), `FakeSupervisor` supervisord-treu (SPAWN_ERROR-Kette, EXITED-Absturzpfad, `start_failure()`, PID-Logik — 3 Treue-Fixes aus blindem GPT-5.6-Review), dbimport-Assertion 2011er-Meta (Task-Chip task_e5db0b72 erledigt); 1701 Tests, E2E 6/6 doppelt, kein Verhaltensunterschied |
| **M1b** | **Ein-Container-Umbau** | 6c4a184 (9 Commits) | supervisord 4.3.0 + tini (Py-3.14 verifiziert; eigenes venv /opt/supervisor), ein Multi-Stage-Dockerfile (PGDG-PG 18.4, fpindex aus Quelle mit Commit-Pin + NOTICES, 462 MB), `process.py`+`stack.py` ersetzen `docker.py` (docker.sock weg), `GroupStatus` statt bool (Absturz ≠ Schlaf), Kante `ready→error`, sequenzieller Start mit Gates (DB hart via pg_isready — gilt auch für schon Laufendes), E15-Politik + Index resident (E12), initdb-Entrypoint (App-Rolle ohne Superuser: CREATEDB+pg_checkpoint; Passwort nur als Datei), API unprivilegiert (User `api`, /config setgid 0640-Entscheid), `/_health`-Deny, eine Compose (Bind-Mounts, stop_grace 6m, Healthcheck /status), Dev-Compose aus dem einen Image, E2E portiert 8/8 (eigener Projekt-Namespace), CI `image-tests`, Kontrakt-Tests gegen echtes supervisord (12), docs/migration-v1-v2.md (ungeprobt!); GPT-Review: 6 Findings gefixt (API-root, Passwort-Leaks inkl. psql-cmdline, Gate-Lücke, observe()-Teilzustand — Alt-Test hatte den Fehler festgeschrieben, E2E-Namespace, Rollback-Doku); 1547 Unit + 199 Integration + 8/8 E2E, alles doppelt |
| **M2** | **Umbenennung musicmeta-offline** | 1fc9f7f (14 Commits, 2 Wellen) | Env-Prefix `AOFF_`→`MMO_` + Config-Keys aufs v2-Schema mit Übergangslesen (E9: `LEGACY_KEYS`-Tabelle statt AliasChoices — Aliase lösen nur je Mapping-Ebene; einmaliger Umschreiber beim Wächter-Start, v1-config-Test), Übergang wirkt auch über die **Compose-Grenze** (beide Namenssätze durchgereicht, `ports:` verschachtelt interpoliert, Layout-Tests; Konsens-HOCH-Finding des blinden Doppel-Reviews Opus+GPT 5.6), `/status` additiv `components` (PG-Major, `MMO_INDEX_COMMIT`), `release.yml` (v*-Tag→GHCR, `latest=auto`, Rename-Guard; imagetools-Finding adversarial **widerlegt** — Fix hätte den Job gebrochen), Entrypoint-Warn-Hygiene + read-only-Secret-Toleranz, ARCHITECTURE/README/4 Betriebs-Docs auf gebauten v2-Stand (§5.1/§5.2 byte-identisch, §12-Nummerierung erhalten — Code zitiert Punktnummern), Rename-Reihenfolge dokumentiert + **GitHub-Rename ausgeführt** (Redirects, Remotes lokal+Tower). Gates: Unit 1597, Integration 199 (eigenes Image), E2E 8/8, CI 2× grün |

---

## M2.5: Scheduler, Notifications, Backup, Metrics (alte Phasen 19–22)

Ziel: Täglicher vollautomatischer Delta-Import inkl. Wecken/Schlafen;
Kanäle ntfy/Webhook + SMTP; zeitgesteuerte Sicherung; optionaler
Prometheus-Endpoint. Danach voller 5-Phasen-Doku-Sweep (M1a–M2.5).

Aufgaben:
- [ ] Scheduler: `acoustid.update.time` → Prozesse wecken →
      Importer-Job als **Wächter-Subprozess** (E10; Argumente,
      returncode, `--report`-Datei) → Ergebnis überwachen →
      `invalidate_cache("delta_import")` → schlafen legen
- [ ] `update_run`-Migration: neue Job-Typen (acoustid-delta,
      discogs-dump, caa-crawl, nachzügler, backup, queue-send) —
      CHECK-Constraint + `RunKind` erweitern; Historie aus dem
      Importer-Report befüllen
- [ ] Fehlgeschlagener Lauf → automatische Wiederholung im nächsten
      Zyklus; `disk.min_free_gb`-Guard je Schreibpfad (E11)
- [ ] Interne Trigger-API für manuelle Läufe (Basis für /admin/jobs)
- [ ] Notify-Modul (ntfy/Webhook + SMTP, leer = aus); Ereignisse:
      Import fehlgeschlagen, Plattenplatz knapp, Stack-Start-Fehler,
      `upstream_forward_gave_up`, Versions-Drift (E14);
      Testnachricht-Funktion je Kanal
- [ ] Backup-Job (`backup.time` → `/backup`, K9): local_submission +
      Wächter-SQLite + config.yaml; `backup.include_covers`-Schalter
      (Default false); zählt als Job (blockiert Idle-Stopp);
      Restore-Anleitung; **lookup-cache.sqlite3 gehört NICHT ins
      Backup**
- [ ] `GET /metrics` (nur bei `metrics.enabled`): Lookups,
      Cache-Quote, Weckvorgänge, Prozess-Zustand, Import-Läufe/-Dauer
- [ ] Compose-Test: simulierter Zyklus inkl. Fehler-Retry-Pfad

Hinweise (aus v1-Phasen, übernommen):
- Beim Stoppen des Importer-Subprozesses großzügige Frist — SIGTERM
  wirkt erst nach der laufenden Tagesdatei (sonst SIGKILL statt
  geordnetem Exit-Code 8).
- Ein Submit während des Update-Laufs erhöht die Index-Version und
  lässt den Index-Feed am `expected_version`-Guard scheitern (Lauf
  endet als Fehler, Resume intakt) — **in dieser Phase entscheiden:**
  Submits während des Laufs zurückstellen ODER Feed ohne Guard.
- Aufrufpunkte für den Update-Lauf: `drain_queue(connection, service,
  limit=…, max_attempts=…)` und `retry_forward(connection, service,
  local_track_ids=…)` (`api/app/upstream.py`; beide werfen bewusst
  durch).
- „Stack-Start-Fehler"-Ereignisse existieren (Quelle `wake`/`stack`,
  Phase 16); ein Stack im `error`-Zustand bleibt bewusst stehen —
  die Notification macht ihn aktiv sichtbar.
- Ereignis `upstream_forward_gave_up` trägt `local_track_id`,
  `forward_attempts`, `forward_error` (Phase 12).

Definition of Done: Zyklus-Test grün; Historie korrekt; Prozesse
schlafen nach dem Lauf; alle vier Notify-Ereignisse feuern in Tests
auf beiden Kanälen; Backup-Tests grün + Restore dokumentiert;
Metrics-Tests grün. Danach 5-Phasen-Doku-Sweep.

## M3: Discogs-Spiegel

**Recherche-Gate zuerst** (§14.2, Bericht nach docs/research/):
exaktes XML-Schema der Monats-Dumps (Referenz discogs-load/
discogs-xml2db), Dump-URL/-Rhythmus für den Verfügbarkeits-Check,
Token-Rate-Limits der Bilder-API. Erst danach Datenmodell.

Aufgaben: Schema `discogs` (neue Migrationen; E8-Schema-Migration
`CREATE SCHEMA acoustid` spätestens hier mit erledigen), Dump-Importer
(etappenweise je Entitätstyp, resumierbar über `dump_state`,
Stale-statt-Sperre), täglicher Verfügbarkeits-Check
(`discogs.update.check_time`), API-kompatibles GET-Subset
(`/discogs/releases|masters|artists|labels/{id}`), Proxy-Routing +
Auth + Fehlerformat der Discogs-API, Cache-Invalidierung
quellen-selektiv (Umbau von „vollständig", DECISIONS 2026-08-04).

Definition of Done: Import eines echten (Teil-)Dumps grün; GET-Subset
antwortformat-verifiziert; Verfügbarkeits-Check getestet.

## M4: Cover-Subsystem

**Recherche-Gate zuerst** (§14.3): offizieller CAA-Bulk-Bezugsweg
(IA-Bulk/rsync — verkürzt den Erst-Crawl), verträgliche Crawl-Rate,
exaktes URL-/Größenschema.

Aufgaben: Schema `covers` (`artwork`, `crawl_state`),
**Beschaffungs-Queue-Infrastruktur** in `shared` (je Quelle eine Queue
mit Drossel/Backoff; CAA-Queue geteilt Crawler+Lazy; Wiederverwendung
der `api/app/upstream.py`-Muster bewerten), Beschaffungskette
CAA → TADB → Discogs (erste Quelle gewinnt), Pillow-Normalisierung
(max. 1200px, JPEG, eine Datei je Release,
`/data/covers/<mbid[0:2]>/<mbid>.jpg`), CAA-kompatibler Endpoint
(`/caa/release/{mbid}/front` + Größen-Suffixe), Negativ-Cache
(`covers.negative_retry_days`), Backup-Erweiterung
(artwork-Metadaten).

Definition of Done: Kette mit Mock-Quellen vollständig getestet;
Endpoint liefert Bild; Negativ-Fall mit `next_retry_at`.

## M5: CAA-Crawler

Aufgaben: Release-Verzeichnis aus dem MB-Spiegel
(`cover_art_archive`-Schema — **neue GRANTS für die Read-only-Rolle
prüfen/erweitern**, Selfcheck + Verfügbarkeitsprüfung §14.5), Drossel
(`caa.crawl.rate_per_s`), Resume über `crawl_state`-Cursor,
abschaltbar (Default aus), täglicher Nachzügler-Job nach dem
AcoustID-Lauf, Crawler zählt als Aktivität (hält wach — via
`JobSource`, §10.5), Notify-Ereignisse (abgeschlossen, festgefahren).

Definition of Done: Crawler-Simulation mit Mock-CAA grün (Resume,
Drossel, Abbruch); Idle-Interaktion getestet.

## M6: TheAudioDB-Proxy-Cache

**Recherche-Gate zuerst** (§14.4): API-Umfang je Key-Klasse,
Rate-Limits, Cache-Erlaubnis laut ToS (Lizenzrisiko §15.2.6).

Aufgaben: transparenter Cache-Proxy `/tadb/…` (Pfadschema der
Original-API, Betreiber-Key, leer = Quelle aus), Ablage
`/data/tadb/…` (JSON + Bilder), Cache unbegrenzt, Invalidierung
einzeln/gesamt (Admin ab M8), 404-Semantik `not_cached_offline`/
`source_disabled`, eigene Beschaffungs-Queue.

Definition of Done: Hit/Miss/Offline/Disabled-Fälle getestet.

## M7: Vereinheitlichte /v1-API

Aufgaben: `mbref`-Modul (MBID↔Discogs über URL-Relationships im
MB-Spiegel — Grants/Selfcheck wie M5), `POST /v1/identify`
(Fingerprint+Duration → AcoustID-Match + MB-Metadaten + Discogs +
TADB + Cover-URL + Score), `GET /v1/release/{mbid}`,
`GET /v1/cover/{mbid}` (löst bei Miss die Kette aus); fehlende
Teilquellen als `null`-Blöcke mit Grund (`not_cached_offline`/
`source_disabled`/`not_found`) — nie Gesamtfehler; Lookup-Cache
quellenübergreifend (v1-Pakete + TADB-Antworten, §6.10).

Definition of Done: Paket-Antworten mit allen Degradationsfällen
getestet; Cache-Verhalten je Quelle verifiziert.

## M8: Admin-UI komplett (alte Phasen 23–27 + v2-Erweiterung)

Referenz: DESIGN_HANDOFF v2 (separate Design-Session; klären, was vom
abgenommenen v1-Paket unter docs/design/ trägt).

Umfang: Grundgerüst + Login (Base-Layout, 6 Bereiche + Logout,
Session-Cookie, Erststart-Hinweis, Zustands-Indikator mit
HTMX-Polling — `schlafend` als Gutzustand), Dashboard (Karten inkl.
Vier-Quellen-Datenstand, Crawler-Steuerung, Prozess-Badges),
Konfiguration (alle §7-Schlüssel, Secrets maskiert, Reload-Felder,
Testnachricht/MB-Test/Cache-leeren-Aktionen), API-Keys & Jobs
(Key-Verwaltung wie v1-Plan; Jobs: Update/Backup/Crawl jetzt, mit
„weckt"-Hinweis, Fortschritt, Abbrechen; Historie mit Filtern), Logs &
Stats (event_log-Ansicht, Kennzahlen + Zeitreihen ohne Build-Schritt).
Jede Fläche mit Sicht-Check (Desktop + schmal).

Hinweise (aus v1): `EVENT_LOG_LIMIT = 5000` (`watchdog/app/events.py`)
— braucht die Logansicht mehr, dort erhöhen. Beim Umschalten
`local` → `local+upstream` anzeigen, dass der nächste
Warteschlangenlauf den gesamten bisher lokalen Bestand nachschiebt
(500 Gruppen je Lauf, ≤ 3 req/s).

Definition of Done: alle Bereiche funktional gegen echte
Wächter-Logik; kein UI-Aufruf weckt das Array; Sicht-Checks bestanden.

## M9: Abschluss — E2E, Clients, Release (alte Phasen 28–29)

Aufgaben: Compose-E2E-Szenarien (schlafend → weckt → idle → schläft;
Update-Zyklus; Batch; Cache-Hit bei schlafenden Prozessen; apikey;
Rate-Limit; neu: /v1-Paket, Cover-Kette, Quellen-Ausfälle), E2E in CI
(Ein-Container macht es billig), Drittclient-Test (Picard/beets per
URL-Umbiegung), DroppedNeedle-Test mit dem Betreiber, erster echter
Upstream-Lauf (Application-Key registrieren, mit EINER Einreichung
beginnen; unbestätigt: Fehlerantwort bei ungültigem Key, Verhalten
> 3 req/s), Batch-Kosten jenseits der 100er-Grenze auf Zielhardware
messen, arm64-Spike (E3: fpindex für aarch64 + 198 Integrationstests
— erst bei Grün ins Release; Achtung: der imagetools-inspect-Schritt
in release.yml gibt bei >1 Plattform still `null` aus — beim
Mehr-Plattform-Umbau anpassen, Verifikationsbefund 2026-08-05),
`release.yml` existiert seit M2 (ein Image → GHCR, `latest=auto`,
Rename-Guard) — hier nur noch Mehr-Plattform + Finalisierung,
README final (Unraid Cache/Array inkl.
„Image + /config auf den Cache", Bootstrap-Zeiten aus den Messungen,
Restore, Lizenzen inkl. GPL-NOTICES, TLS/apikey-Hinweis),
Unraid-Community-App-Template (ein Container, Mounts inkl. /backup,
UID-Matrix), .env.example final, **XFF-Betreiber-Entscheid**
(Rate-Limit hinter TLS-Proxy — Vertrauenslisten-Schlüssel, Default
leer = heutiges Verhalten).

Definition of Done: E2E-Suite grün (CI); mind. ein Drittclient
verifiziert; DroppedNeedle-Test durchgeführt oder terminiert;
Tag-Release baut das Image; Template validiert; README vollständig;
v2-§1-Erfolgskriterien nachweisbar.
