# PROGRESS.md — musicmeta-offline (vormals acoustid-offline)

Übergabe- und Steuerungsdatei: Statuskopf, Session-Übergabe, kompakte
Ergebnisliste der abgeschlossenen Phasen und der vollständige Plan der
offenen Phasen. Die ausführlichen Aufgabenblöcke erledigter Phasen
(inkl. der alten v1-Blöcke 19–29) liegen in der Git-Historie dieser
Datei und im Session-Archiv (`sessions/`). Quelle des Plans:
docs/HANDOFF.md (**v2**, „musicmeta-offline"; v1 archiviert unter
docs/archive/HANDOFF-v1.md) + docs/research/m0-impact-analyse.md;
technische Referenz: ARCHITECTURE.md (beschreibt den gebauten
v2-Stand inkl. M2.5; M3–M8 dort als Plan, siehe Kopfvermerk).

**Status (2026-08-05, abend): Phasen 0–18 (v1), M0, M1a, M1b, M2 und
M2.5 abgeschlossen — die Instanz weckt sich selbst, importiert
täglich, meldet, sichert und misst.** M2.5 (`0dd9fab`, 17 Commits:
9 Bau + 4 Review-Fixes + 4 Gate-Fixes): Scheduler (Fälligkeit „seit
diesem Termin lief keiner" — auch `aborted` verbraucht den Termin,
DECISIONS), Jobs als Wächter-Subprozesse (E10) mit kohärenter
Fristen-Kette 360/300/240 s (stop_grace ≥ stopwaitsecs ≥
SIGTERM-Frist), `update_run`-Migration (6 Job-Arten) + Startup-
Rekonziliation gegen ewig offene Läufe, Notify (ntfy/Webhook + SMTP,
5 Ereignisse, zustandsgetrieben), Backup-Job + Restore-Doku
(lookup-cache bleibt draußen, `.part`-Reste werden geräumt),
`GET /metrics`, Log-Rotation (copytruncate), Disk-Guard je
Schreibpfad inkl. `/index` (E11), Submit-Zurückstellung während des
Laufs (Marke mit 24-h-Ablauf, Nachlauf `queue-send`, Feed-Retry bei
Version-Mismatch) und **Kaltstart-Sperre**: ein unbegrenzter
`update`-Lauf auf leerer Historie bricht mit Bootstrap-Verweis ab
(Betreiber bestätigt — sonst zöge jede frische Instanz um 04:00 die
414-GB-Historie). Doppel-Review blind (Opus 7 + GPT 15 Findings,
6 Konsens; 10 einseitige adversarial verifiziert: 7 bestätigt,
2 widerlegt, 1 herabgestuft; 3 Nebenbefunde) — alles gefixt.
E2E-Fund: Compose-Tests haben NETZ; ein Test lud real echte Deltas
→ Quelle im Test abgeklemmt, Sperre im Produkt. Gates doppelt:
Unit 1805, Integration 210 (eigenes Image), E2E 13/13, CI grün.
**Messlauf auf Tower ABGEBROCHEN** (12:58 UTC, nach 383/3386
Dateien, 19,6 GB gz, 8,1 h: „server closed the connection
unexpectedly … terminated abnormally" — die Tower-PG brach weg,
Exit 6; Resume via `import_state` intakt, Ursache klären).
**Warten auf Go für M3 (Recherche-Gate Discogs zuerst).**

## Session-Übergabe (2026-08-05, M2.5 komplett)

**Kurzbeschreibung:** (1) **Bau:** ein Opus-Agent im Worktree
(9 Commits; Querschnitts-Phase, konfliktfreier Zuschnitt ≈ 1 wie bei
M2). Betreiber-Entscheid vorab: Submits während des Update-Laufs
**zurückstellen** (Guard bleibt), statt Feed ohne Guard oder
Abbruch+Retry. (2) **Doppel-Review blind + adversariale
Verifikation:** Opus 7 + GPT‑5.6 15 Findings → 6 Konsens (u. a.
HOCH: abgebrochene Zyklen hinterlassen ewig offene `update_run`-
Zeilen → Idle-Stopp dauerhaft blockiert; `stopwaitsecs=30` hebelte
die 900-s-SIGTERM-Frist aus; Cache-Invalidierung saß VOR dem
Submit-Nachtrag). 10 einseitige durch 3 Opus-Verifizierer + Fable
(HOCH) geprüft: 7 bestätigt, 2 widerlegt (kind-Umschrieb — Tabelle
nachweislich leer; SQLite-Migrationskonvention korrekt),
1 herabgestuft (Marken-TOCTOU theoretisch). 3 Nebenbefunde der
Verifikation selbst (Hand-Läufe ohne Marke; IdleStopper-Kopplung
ließ den Stack nach Jobs nie einschlafen; ungeschützte mkdir/unlink
im Runner). 5 Fix-Richtungen als schädlich widerlegt und NICHT
gebaut. Nacharbeit: 4 Commits, alles gefixt. (3) **Gates + zwei
Gate-Fix-Runden:** Integration fand 3 Testfehler (CLI ohne
DB-Passwort im Test-Env, Doc-ID-Bereich verwechselt,
DROP-DATABASE-FORCE-Flake im Bestands-Teardown). E2E fand
nacheinander: Termin-Fälligkeits-Missverständnis im Test (Termin lag
VOR dem ersten Lauf), dann den Kernfund — **Compose-Tests haben
Netz**, der Retry-Test lud real echte AcoustID-Deltas ab 2011
(leere `import_state` = ganze Historie). Konsequenz beidseitig:
**Kaltstart-Sperre im Produkt** (unbegrenzter `update`-Lauf auf
leerer Historie → `usage_error` mit Bootstrap-Verweis; Betreiber
bestätigt) und Test-Härtung (`/etc/hosts`-Abklemmung, definierter
Stack-Zustand je Test). (4) **Gates final doppelt:** Unit 1805,
Integration 210, E2E 13/13 (7:05), ruff; Merge ff `0dd9fab`
(+9430/−127), CI grün. (5) **Messlauf Tower:** heute 04:53–12:58 UTC
gelaufen, dann `import_failed` Exit 6 — PG-Server brach weg
(„terminated abnormally"; shfs-Vorgeschichte vom Morgen). 383/3386
Dateien, 17,6 Mio Zeilen; Resume intakt, nichts angefasst.
(6) Prozess-Besonderheit: der Bau-Agent überlebte eine
Session-Rotation nicht — Fortsetzung durch frischen Opus-Agenten
mit self-contained Auftrag im selben Worktree.

**Aktueller Stand — funktioniert (getestet, CI grün):**
Alles aus dem v1-Stand bis Phase 18: Shared (Config/Env/Logging/
Modelle), DB-Migrationen, Index-Client, Importer komplett, API komplett
(/v2/lookup, /v2/submit off/local/local+upstream, Batch,
submission_status — bug-für-bug-dokumentiert), Wächter-Kern
(Grundgerüst/SQLite/Status, Proxy/Prozess-Steuerung/Wecken,
Zustandsmaschine/Idle-Stopp, Lookup-Cache, Auth & Rate-Limit).
Seit M2.5 dazu: Scheduler mit täglichem Zyklus, Jobs als
Wächter-Subprozesse, Notify, Backup + Restore-Doku, `/metrics`,
Log-Rotation, Disk-Guard je Schreibpfad, Kaltstart-Sperre.
Ergebnistabelle unten.

**Existiert noch nicht:** Discogs/Cover/CAA/TADB/v1-API (M3–M7),
Admin-UI (M8), E2E-Suite in CI ausgeweitet/Release (M9). Nie
gelaufen: Voll-Bootstrap am echten Datenbestand (Messlauf ist erst
der Anfang), echter Upstream-Submit, Volume-Migration v1→v2 auf
echter Hardware (R3-Probe offen), erster v*-Release-Tag (bewusst
zurückgestellt).

**Offene Punkte (priorisiert):**
1. **Messlauf-Abbruch auf Tower klären + fortsetzen:** heute
   12:58 UTC `import_failed` Exit 6 nach 383/3386 Dateien — die
   Tower-PG beendete die Verbindung („server terminated abnormally";
   shfs-Absturz-Vorgeschichte vom Morgen). PG-Logs auf Tower
   ansehen, Ursache klären (OOM? Platte? shfs?), Lauf per Resume
   fortsetzen. Report `/mnt/cache/appdata/acoustid-offline/dumps/
   probelauf.json`, Log `…/messlauf.log`. Nichts wegräumen — der
   Stand ist der Anfang des echten Bootstraps.
2. **Go-Entscheidung M3** einholen (Discogs-Spiegel; beginnt mit
   dem Recherche-Gate §14.2, Block unten).
3. **Messlauf auswerten** (sobald durch): query_hashes-
   Empfehlungstabelle + README-Zeitangabe + LEARNINGS-Messwerte
   (PG-Start/Stopp am echten Bestand, Index-Kaltstart auf SSD →
   entscheidet E12-Mess-Vorbehalt + startsecs-Tuning).
4. **Volume-Migrationsrezept proben** (R3): docs/migration-v1-v2.md
   auf Tower am Probelauf-Bestand durchspielen, BEVOR der Bestand
   produktiv gebraucht wird (der neue Container-Entrypoint legt sonst
   ein leeres Cluster an — Abnahme §8 des Rezepts prüft darauf).
5. **README/Setup:** der Bootstrap-Schritt muss ausdrücklich in die
   Anleitung — die Kaltstart-Sperre (DECISIONS 2026-08-05) setzt ihn
   voraus; Fehlertext und Notification verweisen zwar auf
   `--mode bootstrap`, die Anleitung fehlt aber (spätestens
   M9-README).
6. **Neue Betreiber-Entscheide aus den Recherche-Gates** (bei
   M3/M6-Go stellen): Discogs-Bilder-ToS vs. Lazy-Cache (Ⓞ8 in
   docs/research/m3-discogs-dumps.md, Empfehlung liegt bei); TADB-
   Key-Stufe (Empfehlung: Single Developer 8 €/Mon.); MB-GRANTs
   einspielen (Skript in docs/research/m5-mb-spiegel-befund.md §5)
   + Projekt-Container ins MB-Docker-Netz.
7. **Daten-Flaute:** entschärft — Deltas bis 2026-07-27 (geprüft
   04.08.); Export hinkt ~8 Tage nach. Vor Produktivstart erneut
   prüfen.
8. Echter Upstream-Lauf + Drittclient-Tests: bewusst erst M9.
9. **XFF-Betreiber-Entscheid**: spätestens M9.
10. **Erster Release-Tag** (`v*` → GHCR-Image): erst nach
    Messlauf-Auswertung + R3-Probe (Betreiber 2026-08-05). Danach
    GHCR-Paket von Hand öffentlich stellen; alte
    `acoustid-offline-*`-Pakete als „eingestellt" markieren
    (UI-Schritt, Token ohne packages-Scope).

**Nächster konkreter Schritt für eine frische Session:** `git log
--oneline -5` + diesen Statuskopf lesen (Pflicht-Vorprüfung), dann
den Messlauf-Abbruch auf Tower klären (Punkt 1; `ssh Tower`,
Betreiber-Freigabe 2026-08-04). Per AskUserQuestion das Go für
**M3** einholen — M3 beginnt mit dem Recherche-Gate (§14.2, Bericht
nach docs/research/, erst dann Datenmodell/Bau); bei Go den
Recherche-Agenten bzw. Opus-Bau-Agenten mit dem M3-Block unten
beauftragen — inklusive Stand-Vorprüfung und Sperrzonen-Vermerk.
Achtung: Repo-Hooks verlangen `pytest`-Aufrufe ohne Pipe
(`cmd > log; echo rc=$?`) und stellen bei `--compose/--network`
Rückfragen — E2E-Läufe deshalb vom Orchestrator fahren.
Serena-Symboltools NIE in Worktrees verwenden (schreiben ins
Haupt-Repo — LEARNINGS). Compose-E2E-Container haben NETZ — Tests,
die Quellen brauchen, klemmen sie explizit ab (LEARNINGS M2.5).

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
| **M2.5** | **Scheduler, Notify, Backup, Metrics** | 0dd9fab (17 Commits) | Täglicher Zyklus: Wecken → Importer als Wächter-Subprozess (E10) → Cache-Invalidierung → `queue-send`-Nachlauf → Schlafen (nur wenn selbst geweckt + unbenutzt; `ActivityTracker.defer()` trennt Job-Aufschub vom Anfragezähler — sonst schlief der Stack nach Jobs nie). Fälligkeit „seit Termin lief keiner" (auch `aborted` verbraucht; neuer Termin nach dem Lauf = neue Gelegenheit). `update_run`-Migration (6 Job-Arten) + Startup-Rekonziliation (keine ewig offenen Läufe; Backup-Kopie wird nachgezogen), Fristen-Kette 360/300/240 s testgekoppelt, Notify ntfy/Webhook+SMTP (5 Ereignisse, zustandsgetrieben, gave-up nur bei Neuzugang), Backup (local_submission + SQLite + config.yaml via Online-Backup-API, ohne lookup-cache; `.part`-Räumung; docs/backup-restore.md), `/metrics` (nur bei `metrics.enabled`), Log-Rotation copytruncate, Disk-Guard je Schreibpfad inkl. `/index` aus `ACOUSTID_INDEX_DIR` (E11), Submit-Zurückstellung (Marke mit 24-h-Ablauf; Feed heilt Version-Mismatch 2×), **Kaltstart-Sperre** (unbegrenzter `update` auf leerer `import_state` → `usage_error` mit Bootstrap-Verweis, Betreiber bestätigt). Doppel-Review blind Opus 7 + GPT 15 → 6 Konsens, 7 bestätigt, 2 widerlegt, 1 herabgestuft, 3 Nebenbefunde, 5 schädliche Fix-Richtungen verworfen; E2E-Fund „Compose-Tests haben Netz" (Test lud echte Deltas → Quelle abklemmen + Produkt-Sperre). Gates: Unit 1805, Integration 210 (eigenes Image), E2E 13/13, CI 2× grün |

---

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
