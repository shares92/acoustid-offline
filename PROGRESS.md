# PROGRESS.md — acoustid-offline

Übergabe- und Steuerungsdatei: Statuskopf, Session-Übergabe, kompakte
Ergebnisliste der abgeschlossenen Phasen und der vollständige Plan der
offenen Phasen. Die ausführlichen Aufgabenblöcke erledigter Phasen
liegen in der Git-Historie dieser Datei und im Session-Archiv
(`sessions/`). Quelle des Plans: docs/HANDOFF.md; technische Referenz:
ARCHITECTURE.md.

**Status (2026-08-01): Phasen 0–18 abgeschlossen — Auth (`apikey`)
und IP-Rate-Limit stehen am Proxy, auch für Cache-Hits bei
schlafendem Stack. Repo https://github.com/shares92/acoustid-offline,
Phase-18-Commit `2570ea5` (danach Doku-Update), Arbeitsbaum sauber,
CI grün (1649 Tests, drei Jobs: Lint+Unit 1391+43 übersprungen,
Integration PG+Index 198, Bit-Verifikation pg_acoustid 8; 3 network-
und 6 compose-Tests laufen nie in CI, der Compose-E2E lief lokal
zweifach grün). Warten auf Go für Phase 19 (Wächter — Scheduler &
Update-Zyklus); danach ist der volle 5-Phasen-Doku-Sweep (15–19)
fällig.**

## Session-Übergabe (Session-Ende 2026-08-01, nach Phase 18)

**Kurzbeschreibung:** Session vom 01.08. (Wächter-Kern): Phasen 14–18
nacheinander gebaut (je ein Opus-Bau-Agent im Worktree,
Stand-Vorprüfung jeweils bestanden), vom Orchestrator verifiziert
(Code-Review + Suite + ruff; E2E ab Phase 15 jeweils doppelt gefahren
— Agent und Orchestrator), ff-Merges
`7ce2cd5`/`3f9daee`/`a42c192`/`8c3816c`/`2570ea5`, alle CI-Läufe
beobachtet und grün, 5-Phasen-Doku-Sweep (10–14) und Doku-Updates
nach 15–18 erledigt. Session-Archiv:
`sessions/2026-08-01-phasen-14-18-waechter-kern.md`. **Die Go-Frage
zu Phase 19 und die XFF-Frage wurden am Session-Ende verworfen — kein
Go erteilt, kein Entscheid gefallen.**

**Aktueller Stand — funktioniert (getestet, CI grün):** Shared-Paket
(Config/Env/Logging/Modelle), DB-Migrationen (core/indexes),
Index-Client (msgpack), Importer komplett (Download, Parser mit
Epochen-Lesart, transaktionaler Import, Index-Feed, Bootstrap-Job mit
9 Exit-Codes und Probelauf-Modus), API komplett (/v2/lookup mit
meta/MB-Resolver, /v2/submit off/local/local+upstream mit
Upstream-Queue, /v2/lookup/batch, /v2/submission_status) — alles
bug-für-bug-kompatibel dokumentiert (docs/api-lookup.md,
docs/api-submit.md, docs/importer-job.md). **Wächter (Phasen 14–15):**
Grundgerüst `acoustid_watchdog` (SQLite-Zustandsdatenbank mit
`user_version`-Migrationsläufer, event_log-Ringpuffer 5000, `GET
/status` baulich weckfrei, Erststart mit argon2-Passwort nur ins
Containerlog), Reverse-Proxy `/v2/*` (streamend, roh — 405-Eigenheit
bleibt durchgereicht), Docker-Steuerung über den Socket (3 Routen,
httpx über uds, keine Fremdbibliothek), Wake-on-request
(Einzel-Weckvorgang via Task+shield, Timeout → 503+Retry-After 30,
E2E-verifiziert: Weckdauer ~1,3 s lokal), interner API-Healthcheck
`GET /_health` (DB+Index, ohne MB), Reload-Signal komplett
(Sendeseite Marke `config.yaml.reload` + Empfangsseite
`api/app/reload.py`, 10-s-Intervall, Teilmenge submit.*/
mb.keep_submitted_mbid). **Phase 16:** Zustandsmaschine mit
Übergangstabelle (`ALLOWED_TRANSITIONS`, streng `to()` vs. nachsichtig
`try_to()`), Idle-Stopp (`idle.timeout_min`, Job-Blockade §8.5 über
`JobSource`/`update_run`, Job setzt Leerlaufuhr zurück), Docker-Poller
(15 s, erkennt Hand-Stopp/-Start, überspringt bei laufenden
Vorgängen), Startfehler-Pfad mit Erholung aus `error`, Weck-Frist
gehört jetzt dem Vorgang (jeder Wartende verlängert auf seine
Haltezeit). **Phase 17:** Lookup-Cache als eigene SQLite-Datei
(`lookup-cache.sqlite3`, selbstheilend — Defekt ⇒ wegwerfen und neu),
Schlüssel = SHA-256 per Sperrliste (alles außer `client`/
`clientversion`), nur HTTP 200 + `status: ok` wird eingelagert
(Batch/xml/jsonp bewusst nicht), Byte-Parität ohne `X-Cache`,
LRU-Verdrängung über monotone Sequenz bis 90 % von
`cache.max_size_mb`, Hit zählt nicht als Aktivität, Invalidierung
über `service.invalidate_cache(reason)` (Submit im Proxy-Pfad;
`delta_import` Phase 19, `manual` Phase 25) — auch bei
`cache.enabled=false`. **Phase 18:** Reihenfolge Rate-Limit → Auth →
Cache → Wecken (abgewiesene Anfragen wecken nie, Tripwire-getestet);
`apikey`-Modus gegen `api_key`-Tabelle (sha256-Hash-Vergleich
konstant-zeitig, „zuletzt benutzt" auf 1 Schreibvorgang/60 s je Key
gedrosselt), Whitelist-Schalter Picard/beets, Fehlercodes belegt
(fehlender client 2/400, ungültiger Key 4/400, Limit 14/429 mit
gerechnetem Retry-After, Rumpf > 1 MiB 19/413); IP-Limiter als
exaktes gleitendes 60-s-Fenster (LRU-Deckel 2048 IPs, abgelehnte
Anfragen zählen nicht); 503-Text jetzt generisch (Original-Wortlaut
Code 13), /status bleibt offen.
**Existiert noch nicht:** Wächter-Rest (Phasen 19–22: Scheduler,
Notify, Backup, Metrics), Admin-UI (23–27, Designpaket abgenommen
unter docs/design/), E2E/Release (28–29). Nie gelaufen:
Voll-Bootstrap am echten Datenbestand, echter Upstream-Submit.

**Offene Punkte (priorisiert):**
1. **Go-Entscheidung Phase 19** einholen (Scheduler & Update-Zyklus;
   Hinweise im Phasenblock: Stop-Timeout, Submit-während-Lauf-Entscheid,
   drain_queue/retry_forward, Cache-Invalidierung) — die Go-Frage vom
   01.08. wurde verworfen, kein Go erteilt. Nach Phase 19 ist der
   volle 5-Phasen-Doku-Sweep (15–19) fällig.
2. **Unraid-Probelauf** (Phase-8-DoD-Rest, Betreiber-Hardware):
   Anleitung docs/probelauf-unraid.md; Report-JSONs zurückgeben →
   daraus query_hashes-Empfehlungstabelle + README-Zeitangabe.
3. **Task-Chip offen** (task_e5db0b72): DB-Assertion für die
   entescapte 2011er-Meta-Zeile in den dbimport-Integrationstests
   (`importer/tests/`; manuell am 01.08. bereits bestätigt).
4. **Daten-Flaute seit 2026-07-22** bei data.acoustid.org vor
   Produktivstart erneut prüfen (ARCHITECTURE §12).
5. Echter Upstream-Lauf + Drittclient-Tests: bewusst erst Phase 28
   (Vormerkungen stehen in den Phasenblöcken).
6. **XFF-Betreiber-Entscheid** (Rate-Limit hinter TLS-Proxy,
   Vertrauensliste als §6-Schlüssel?) — Frage am 01.08. verworfen;
   Hinweis steht im Phase-29-Block, spätestens dort entscheiden.

**Nächster konkreter Schritt für eine frische Session:** `git log
--oneline -5` + diesen Statuskopf lesen (Pflicht-Vorprüfung, s.
Arbeitsregeln), dann per AskUserQuestion das Go für Phase 19 einholen
und bei Go einen Opus-Bau-Agenten mit dem Phase-19-Block unten
beauftragen — inklusive Stand-Vorprüfung im Auftragstext (DECISIONS
2026-08-01). Nach Phase 19: voller 5-Phasen-Doku-Sweep (15–19) mit
Diff-Anzeige.

**Fallstricke — nicht ändern / beachten:**
- ARCHITECTURE-§5.2-DDL und §5.1-Ströme-Tabelle sind **testgekoppelt**
  (anweisungsgleich mit Migrations-SQL bzw. deckungsgleich mit
  Parser-SPECS) — nie freihändig editieren.
- Bug-für-Bug-Paritäten der API sind Absicht (Abweichungstabellen in
  docs/api-lookup.md / docs/api-submit.md) — vermeintliche
  „Original-Bugs" nicht fixen.
- `compare2`/`extract_query`/Chromaprint-Encoder nur mit
  Bit-Verifikation (CI-Job `extension`) ändern.
- Index-Image bleibt per Digest gepinnt; Doc-ID-Bereich für
  Submissions ist [2^31, 2^32-1] (u32-Grenze real).
- Fixtures (`tests/fixtures/acoustid-dumps/*.jsonl.gz`) nie committen
  (Lizenzentscheid); Beschaffung über fetch_fixtures.py.
- Skripte/Container nie mit CWD=Repo-Root starten
  (Namespace-Paket-Falle); psycopg wird in shared bewusst lazy geladen.
- Lokale Integrationstests auf Apple Silicon brauchen colima mit
  `--vz-rosetta`; CI-Flakes durch Docker-Hub-Timeouts → `gh run rerun
  --failed` genügt.

## Arbeitsregeln

- Session-Start: zuerst ARCHITECTURE.md und PROGRESS.md lesen, dann
  fragen, welche Phase dran ist. **Zusätzlich seit 2026-08-01: vor
  jedem Bau-Agenten-Start `git log` + Statuskopf prüfen; Bau-Aufträge
  enthalten die Vorprüfung „Phase bereits umgesetzt? → nicht bauen,
  sondern prüfen und melden" (DECISIONS 2026-08-01).**
- Implementierung einer Phase erst nach explizitem Go des Auftraggebers.
- Keine Annahmen außerhalb des Handoffs — bei Unklarheit nachfragen.
- Nach jeder abgeschlossenen Phase: Statusvermerk hier, Doku aktuell
  halten, bevor die nächste Phase beginnt; danach Pause + Go-Frage.
- Nach Abschluss jeder 5. Phase: PROGRESS.md, DECISIONS.md und (falls
  relevant) ARCHITECTURE.md und LEARNINGS.md eigenständig aktualisieren
  und die Diffs zeigen, bevor es weitergeht. (Nächster Sweep: nach
  Phase 19.)
- Jede UI-Phase endet mit Sicht-Check am gerenderten Ergebnis
  (Screenshot vs. Design, Desktop + schmale Breite).
- Admin-UI (Phasen 23–27): Das abgenommene Designpaket unter
  docs/design/ (inkl. Prototyp-Runtime support.js, verifiziert) ist
  die Referenz — Designentscheidungen werden nicht neu getroffen.

---

## Ergebnisse der abgeschlossenen Phasen 0–14

Vollständige Aufgabenblöcke: Git-Historie dieser Datei (bis Commit
`abd1225`); Entscheide: DECISIONS.md; Berichte: docs/research/.

| Phase | Titel | Commit(s) | Kernergebnis |
|---|---|---|---|
| 0 | Recherche Dump-Format & Bootstrap | — | Kein Voll-Snapshot: Replay aller Tagesdeltas seit 2011-08-19 (414 GB gz, 38.178 Dateien); exakte Schemata (§5.1/§5.2); Fixtures |
| 1 | Recherche Index/Upstream/MB | — | acoustid-index ausgeleuchtet (Cache-Pool-Entscheid), API-Vertrag inkl. `/v2/submission_status`-Korrektur, MB-Query-Schicht-Entwurf (docs/research/) |
| 2 | Repo-Grundgerüst & CI | 15c78e4, 6fde05c | uv-Workspace Python 3.14, öffentliches Repo, CI (setup-uv-Tag-Fix), MIT, fetch_fixtures.py |
| 3 | Shared: Config/Modelle/Logging | 481315c | Alle §6-Schlüssel (pydantic, Secrets 0600, YAML-Sexagesimal-Fix), EnvSettings, JSON-Logging |
| 4 | DB-Schema & Migrationen | 1f294c4 | Eigener Runner, Gruppen core/indexes, lz4 in core, postgres:18; DDL testgekoppelt an §5.2 |
| 5 | Index-Client | 620aa5a | msgpack-Client (query/wire/client/errors), Digest-Pin, AOFF_INDEX_NAME, 12-Befunde-Addendum |
| 6 | Importer: Download & Parser | 0893abe | Epochen-Lesart COPY-Escaping (≤2024-12-04, HOCH), Range-Resume (iter_raw), Arbeitsliste mit Lücken-Meldung |
| 7 | Importer: DB-Import & Index-Feed | 85d7d40 | Datei=Transaktion inkl. import_state, Resume, Feed 1000er nach id; **nachverifiziert 2026-08-01** (1338 Tests) |
| 8 | Bootstrap, Guard & One-Shot-Job | c915fb8 | Bulk-Pfad (core→Import→indexes→Feed), 9 Exit-Codes, Report-Schema, Probelauf-Modus; **DoD-Rest: Unraid-Lauf offen** |
| 9 | API: /v2/lookup Kern | 027597c | Match-Pipeline mit compare2-Nachbau, Bit-Verifikations-CI (fand Phase-5-Fehler), docs/api-lookup.md |
| 10 | API: MB-Resolver & meta | a467064, ea008a6 | shared/mb (Circuit-Breaker, Selfcheck, Staleness), meta bug-für-bug, Online-Redirects, degradierter Betrieb |
| 11 | API: /v2/submit off/local | ead4790, b15c60b | local_submission, synchrone Indexierung, Doc-ID u32-Finding [2^31, 2^32-1], docs/api-submit.md |
| 12 | API: Upstream & Queue | 657ee14 | drain_queue/retry_forward, ≤3 req/s, 7-Fehler-Grenze → upstream_forward_gave_up, Mock-Upstream-Tests |
| 13 | API: Batch & submission_status | 1d8874a | queries/responses-Vertrag (Teilfehler bei 200, Limit 100→19/413, meta-Bündelung); Status nie 404 |
| 14 | Wächter: Grundgerüst, SQLite & /status | 7ce2cd5 | Paket `acoustid_watchdog` (FastAPI), SQLite-Migrationsläufer (`PRAGMA user_version`), event_log-Ringpuffer 5000 exakt, /status baulich weckfrei, Erststart-Passwort argon2 nur ins Containerlog, Reload-Marke `config.yaml.reload`, compose+Healthcheck; am echten Container verifiziert |
| 15 | Wächter: Proxy, Docker-Steuerung & Wecken | 3f9daee | Reverse-Proxy `/v2/*` roh/streamend (405-Parität bleibt), DockerClient (3 Routen, httpx über uds, unversioniert), WakeCoordinator (ein Weckvorgang via Task+shield; Timeout → 503+Retry-After 30, Zustand bleibt `starting`), API `GET /_health` (DB+Index, ohne MB) + Reload-Empfang (10 s, Teilmenge, Rest zurückgeschrieben+Warnung), E2E-Wecktest (Marker `compose`, opt-in): Weckdauer ~1,3 s lokal |
| 16 | Wächter: Zustandsmaschine, Idle-Stopp & Startfehler | a42c192 | `ALLOWED_TRANSITIONS` (25 Paare getestet; streng `to()` / nachsichtig `try_to()`), IdleStopper (30-s-Takt, Job-Blockade §8.5 via `JobSource`→`update_run`, Job setzt Leerlaufuhr zurück), StatePoller (15 s, Hand-Stopp/-Start erkannt, skip bei `busy`, `error` bleibt sichtbar), Weck-Frist gehört dem Vorgang, Anfragen bei `stopping` warten und wecken danach; E2E 4/4 |
| 17 | Wächter: Lookup-Cache | 8c3816c | Eigene SQLite (`lookup-cache.sqlite3`, selbstheilend), Schlüssel SHA-256 per Sperrliste (ohne `client`/`clientversion`, gzip-Rumpf nur für Schlüssel entpackt), nur 200+`status: ok` (Batch/xml/jsonp nicht), Byte-Parität ohne `X-Cache`, LRU über monotone Sequenz (90 %-Räumung), Hit ≠ Aktivität, `invalidate_cache(reason)` submit/delta_import/manual, wirkt auch bei `enabled=false`; E2E 5/5 (Hit 0,01 s bei gestopptem Stack) |
| 18 | Wächter: Auth & Rate-Limit | 2570ea5 | Reihenfolge Limit → Auth → Cache → Wecken (Abweisungen wecken nie, Tripwire); `apikey` gegen `api_key` (sha256 konstant-zeitig, zuletzt-benutzt gedrosselt 60 s), Whitelist Picard/beets, Codes belegt (2/400, 4/400, 14/429 gerechneter Retry-After, 19/413); Limiter exaktes 60-s-Gleitfenster (LRU 2048 IPs, Abweisungen zählen nicht); 503-Text generisch; E2E 6/6 (apikey schützt Cache-Hit bei gestopptem Stack) |

---

## Phase 19: Wächter — Scheduler & Update-Zyklus

Ziel: Täglicher, vollautomatischer Delta-Import inkl. Wecken und
Wieder-Einschlafen.

Aufgaben:
- [ ] Scheduler: `update.time` → Stack wecken → Importer-Job starten →
      Ergebnis überwachen → Cache invalidieren → Stack schlafen legen
- [ ] `update_run`-Historie aus dem Importer-Report befüllen
- [ ] Fehlgeschlagener Lauf → automatische Wiederholung im nächsten
      Zyklus (Invariante §8.4); Plattenplatz-Guard-Ergebnis behandeln
- [ ] Interne Trigger-API für manuelle Läufe (Basis für /admin/jobs)
- [ ] Compose-Test: simulierter Zyklus inkl. Fehler-Retry-Pfad

Hinweis (aus Phase 17): Die Cache-Invalidierung nach erfolgreichem
Delta-Import ist ein Aufruf von
`service.invalidate_cache(reason="delta_import")` — Schnittstelle
steht, Ereignis inklusive.

Definition of Done: Zyklus-Test grün; Historie korrekt; Stack schläft
nach dem Lauf wieder.

Hinweis (aus Phase 8): Beim Stoppen des Importer-Containers ein
großzügiges Stop-Timeout setzen — SIGTERM wirkt erst nach der laufenden
Tagesdatei; Dockers 10-s-Default führt zu SIGKILL (sicheres Rollback,
aber der Lauf endet ohne den geordneten Exit-Code 8).

Hinweis (aus Phase 11): Ein Submit während des Update-Laufs erhöht die
Index-Version und lässt den Index-Feed des Importers am
`expected_version`-Guard scheitern (Lauf endet als Fehler, Resume
intakt — DECISIONS „Phase-7-Import-Details" Punkt 7). In dieser Phase
entscheiden: Submits während des Laufs im Wächter zurückstellen ODER
den Feed ohne Guard fahren.

Hinweis (aus Phase 12): Die beiden Aufrufpunkte für den Update-Lauf
sind `drain_queue(connection, service, limit=…, max_attempts=…)` und
`retry_forward(connection, service, local_track_ids=…)`
(`api/app/upstream.py`; `MAX_FORWARD_ATTEMPTS` exportiert). Beide
werfen bewusst durch, damit der Lauf Fehler sieht — anders als der
Anfragepfad.

## Phase 20: Benachrichtigungen

Ziel: ntfy/Webhook und SMTP, einzeln schaltbar.

Aufgaben:
- [ ] Notify-Modul: ntfy/Webhook-URL + SMTP (Host, Port, User, Pass,
      From, To); leer = Kanal aus
- [ ] Ereignisse anbinden: Import fehlgeschlagen, Plattenplatz knapp,
      Stack-Start-Fehler, Upstream-Submit dauerhaft fehlgeschlagen
- [ ] Testnachricht-Funktion je Kanal (für /admin/config)
- [ ] Tests mit Mock-HTTP/Mock-SMTP je Kanal und Ereignis

Definition of Done: Alle vier Ereignisse feuern in Tests auf beiden
Kanälen; Testnachricht-Funktion vorhanden.

Hinweis (aus Phase 12): Der Auslöser für „Upstream-Submit dauerhaft
fehlgeschlagen" ist das ERROR-Ereignis `upstream_forward_gave_up`
(Felder `local_track_id`, `forward_attempts`, `forward_error`).

Hinweis (aus Phase 16): Für „Stack-Start-Fehler" existieren bereits
die ERROR-Ereignisse `Stack-Start fehlgeschlagen` und `Stack-Stopp
fehlgeschlagen` (Quelle `wake`) sowie die Zustandswechsel-Ereignisse
(Quelle `stack`) — die Notification hängt sich dort an. Ein Stack im
`error`-Zustand bleibt bewusst stehen (kein Idle-Stopp, Entscheid
2026-08-01), bis der nächste Weckversuch ihn auflöst — der Betreiber
sieht den Fehler in /status, die Notification macht ihn aktiv
aufmerksam.

## Phase 21: Backup-Job

Ziel: Zeitgesteuerte Sicherung der lokalen Unikate.

Aufgaben:
- [ ] Backup-Job (`backup.time`): `local_submission`-Daten +
      Wächter-SQLite → `backup.dir`; leeres Ziel = Backup aus
- [ ] Lauf-Historie in `update_run` (Typ Backup)
- [ ] Idle-/Job-Interaktion: Backup zählt als laufender Job
      (blockiert Idle-Stopp)
- [ ] Restore-Anleitung für die Doku (README-Baustein)
- [ ] Tests: konsistente Sicherung, Historieneintrag, deaktivierter Fall

Definition of Done: Backup-Tests grün; Restore dokumentiert.

Hinweis (aus Phase 17): `lookup-cache.sqlite3` gehört ausdrücklich
NICHT ins Backup (Wegwerfware, potenziell 512 MiB); gesichert wird
nur `watchdog.sqlite3` + config.yaml (+ local_submission-Daten).

## Phase 22: Metrics

Ziel: Optionaler Prometheus-Endpoint im Wächter.

Aufgaben:
- [ ] `GET /metrics` (Prometheus-Format), nur bei `metrics.enabled`
      (Default aus)
- [ ] Kernmetriken: Lookups, Cache-Hits/-Quote, Weckvorgänge,
      Stack-Zustand, Import-Läufe/-Dauer/-Ergebnis
- [ ] Tests: Exposition valide; deaktiviert → nicht erreichbar

Definition of Done: Metrics-Tests grün.

## Phase 23: Admin-UI — Grundgerüst & Login

Referenz: abgenommenes Designpaket unter docs/design/.

Ziel: Basis-Layout, Navigation, Login, Statusindikator.

Aufgaben:
- [ ] Base-Layout + Navigation (6 Bereiche + Logout) gemäß Design;
      CSS ohne Build-Schritt
- [ ] Login `/admin/login`: Passwortfeld, Fehlermeldung,
      Rate-Limit-Hinweis nach Fehlversuchen, Erststart-Hinweis
      (Passwort im Container-Log); Session-Cookie
- [ ] Stack-Zustands-Indikator auf jeder Seite (HTMX-Polling 5 s);
      `schlafend` als guter Zustand gestaltet
- [ ] Sicht-Check: Screenshot vs. Design (Desktop + schmal)

Definition of Done: Login + Navigation funktional; kein UI-Aufruf weckt
das Array; Sicht-Check bestanden.

## Phase 24: Admin-UI — Dashboard

Ziel: Statuszentrale gemäß DESIGN_HANDOFF §4.2.

Aufgaben:
- [ ] Fünf Karten: Stack-Status (zustandsabhängige Buttons `Wecken`/
      `Jetzt schlafen legen`/`Neu starten` mit Bestätigungsdialog),
      Datenstand, Aktivität, System, Letzte Ereignisse
- [ ] Zustands-Badges: Import läuft (x von y), Backup läuft,
      Upstream-Queue: N, MB nicht erreichbar, Plattenplatz knapp
- [ ] Alle fünf Stack-Zustände + Badges mit Fixture-Zuständen prüfbar
- [ ] Sicht-Check

Definition of Done: Alle Zustände darstellbar und korrekt verdrahtet;
Sicht-Check bestanden.

## Phase 25: Admin-UI — Konfiguration

Ziel: config.yaml vollständig über die UI editierbar.

Aufgaben:
- [ ] Formulare aller Gruppen (API & Auth, Submit, Betrieb, Cache,
      Benachrichtigungen, Backup, MusicBrainz, Admin-Passwort)
- [ ] Inline-Validierung; Secrets maskiert („ändern" statt Anzeige);
      Reload-auslösende Felder gekennzeichnet + API-Reload-Signal
- [ ] Aktions-Buttons: Testnachricht ntfy/SMTP, MB-Verbindungstest
      (weckt nicht), Cache jetzt leeren
- [ ] Sicht-Check

Definition of Done: Roundtrip UI → config.yaml → UI fehlerfrei; alle
§6-Schlüssel erreichbar; Sicht-Check bestanden.

Hinweis (aus Phase 12): Beim Umschalten `local` → `local+upstream` am
Schalter anzeigen, dass der nächste Warteschlangenlauf den gesamten
bisher nur lokalen Bestand nachschiebt (500 Gruppen je Lauf,
≤ 3 req/s).

## Phase 26: Admin-UI — API-Keys & Jobs

Ziel: Key-Verwaltung und manuelle Job-Steuerung.

Aufgaben:
- [ ] Keys: Tabelle (Label, maskierter Key, aktiv, erstellt, zuletzt
      benutzt); Erzeugen mit einmaliger Klartext-Anzeige + Kopieren;
      (De)aktivieren; Löschen mit Bestätigung; im Modus `none` bedienbar
      mit Hinweis
- [ ] Jobs-Aktionen: Update/Backup jetzt, Upstream-Queue senden — je
      mit „weckt das Array"-Hinweis + Bestätigung; Fortschritt per
      Polling; Abbrechen-Button
- [ ] Jobs-Historie: `update_run`-Tabelle mit Filtern (Typ, Ergebnis),
      aufklappbare Fehlermeldung
- [ ] Sicht-Check

Definition of Done: Aktionen laufen gegen die Wächter-Logik (Phase 19/21);
Historie + Filter funktional; Sicht-Check bestanden.

## Phase 27: Admin-UI — Logs & Statistiken

Ziel: Ereignis-Log und Kennzahlen sichtbar machen.

Aufgaben:
- [ ] Logs: `event_log`-Ansicht (Zeit, Level, Quelle, Nachricht),
      Filter Level/Quelle/Freitext, Auto-Refresh umschaltbar
- [ ] Stats: Kennzahlen-Kacheln (Tracks, Fingerprints, lokale
      Submissions, Datenstand) + Zeitreihen (Lookups/Tag,
      Cache-Hit-Quote, Weckvorgänge/Tag, Import-Dauer, DB-/Index-Größe)
      mit Chart-Lösung gemäß Design-Entscheid (kein Build-Schritt)
- [ ] Datenerfassung ergänzen, wo nötig (DB-/Index-Größe beim
      Update-Lauf, sofern nicht schon in Phase 19 erfasst)
- [ ] Sicht-Check

Hinweis (aus Phase 14): Das Ereignis-Log ist auf
`EVENT_LOG_LIMIT = 5000` Einträge begrenzt (Konstante in
`watchdog/app/events.py`, kein §6-Schlüssel) — braucht die Logansicht
mehr Historie, dort erhöhen.

Definition of Done: Beide Seiten funktional mit echten Wächter-Daten
(Fixtures für Zeitreihen); Sicht-Check bestanden.

## Phase 28: End-to-End & Client-Kompatibilität

Ziel: Gesamtsystem-Nachweis inkl. echter Clients.

Aufgaben:
- [ ] Compose-E2E-Szenarien: schlafend → Lookup weckt → Antwort →
      idle → schläft; täglicher Update-Zyklus; Batch; Cache-Hit bei
      schlafendem Stack; apikey-Modus; Rate-Limit
- [ ] Drittclient-Test: Picard und/oder beets per URL-Umbiegung gegen
      die Instanz
- [ ] DroppedNeedle-Test mit dem Betreiber koordinieren
- [ ] E2E-Suite in CI integrieren (soweit ohne Array-Hardware machbar)

Definition of Done: E2E-Suite grün; mindestens ein Drittclient
verifiziert; DroppedNeedle-Test durchgeführt oder terminiert.

Hinweis (aus Phase 12): Erster echter Upstream-Lauf gehört hierher —
Application-Key registrieren (acoustid.org/new-application, sofort
aktiv) und mit EINER Einreichung beginnen; unbestätigt sind die exakte
Fehlerantwort bei ungültigem Key und das Verhalten oberhalb von
3 req/s (docs/api-submit.md, offene Punkte).

Hinweis (aus Phase 13): Der Batch hat jenseits der 100er-Grenze keine
Kostenbremse (100 × 40 Kandidaten Rescoring synchron im Threadpool) —
Dauer auf der Zielhardware hier mitmessen.

## Phase 29: Release, README & Unraid-Template

Ziel: Veröffentlichbares Gesamtpaket.

Aufgaben:
- [ ] release.yml: drei Images (watchdog, api, importer) → GHCR mit
      einem gemeinsamen Release-Tag (Invariante §8.11)
- [ ] README final: Unraid-Setup (Array/Cache-Zuordnung), generisches
      Docker-Setup, Bootstrap-Anleitung mit realistischer Zeitangabe
      (aus Phase 0/8), Restore, Lizenzhinweis, TLS/apikey-Hinweis bei
      Exponierung

Hinweis (aus Phase 18, offener Betreiber-Entscheid): Hinter einem
TLS-Reverse-Proxy sehen alle Clients für das Rate-Limit dieselbe IP
(X-Forwarded-For wird bewusst nicht ausgewertet — fälschbar).
Empfehlung des Bau-Agenten: Vertrauenslisten-Schlüssel in §6 (Default
leer = heutiges Verhalten). Spätestens hier beim TLS-Hinweis
entscheiden und dokumentieren.
- [ ] Unraid-Community-App-Template (XML) im Repo
- [ ] .env.example final abgleichen

Definition of Done: Tag-Release baut drei Images mit identischem Tag;
Template validiert; README vollständig; Erfolgskriterien aus
ARCHITECTURE.md §1 nachweisbar erfüllt.
