# PROGRESS.md — acoustid-offline

Phasenplan als Checkliste. Quelle: docs/HANDOFF.md; technische Referenz:
ARCHITECTURE.md.

**Status: Phasen 0–5 abgeschlossen (2026-07-25). Repo öffentlich unter
https://github.com/shares92/acoustid-offline, CI grün (347 Tests, davon
51 Integration gegen echtes Postgres 18 + echtes acoustid-index-Image).
Warten auf Go für Phase 6. Phasen 23–27 (Admin-UI) sind blockiert, bis
das Ergebnis der Claude-Design-Session vorliegt; Design-Entscheidungen
werden nicht vorweggenommen.**

## Arbeitsregeln

- Session-Start: zuerst ARCHITECTURE.md und PROGRESS.md lesen, dann
  fragen, welche Phase dran ist.
- Implementierung einer Phase erst nach explizitem Go des Auftraggebers.
- Keine Annahmen außerhalb des Handoffs — bei Unklarheit nachfragen.
- Nach jeder abgeschlossenen Phase: Haken + kurzer Statusvermerk hier,
  Doku aktuell halten, bevor die nächste Phase beginnt.
- Nach Abschluss jeder 5. Phase: PROGRESS.md, DECISIONS.md und (falls
  relevant) ARCHITECTURE.md und LEARNINGS.md eigenständig aktualisieren
  und die Diffs zeigen, bevor es weitergeht.
- Jede UI-Phase endet mit Sicht-Check am gerenderten Ergebnis
  (Screenshot vs. Design, Desktop + schmale Breite).
- Phasen 23–27 sind BLOCKIERT, bis das Ergebnis der separaten
  Claude-Design-Session (auf Basis docs/DESIGN_HANDOFF.md) vorliegt;
  alles Designbezogene wird bis dahin zurückgestellt, nicht
  vorentschieden (Betreiber-Vorgabe 2026-07-25).

---

## Phase 0: Recherche — Dump-Format & Bootstrap-Strategie

Ziel: Das größte Projektrisiko (unverifiziertes Delta-Format, unklarer
Erst-Import) beseitigen, bevor Code entsteht. Reine Recherche.

Aufgaben:
- [x] data.acoustid.org sichten: Delta-Dateien (Benennung,
      Sequenznummern, Veröffentlichungsrhythmus, Kompression) dokumentiert
      (ARCHITECTURE §5.1)
- [x] Tabellen-/Feldstruktur der JSON-Deltas vollständig erfasst —
      dreifach belegt (Exporter-Code, Live-Daten, Fixtures); 7 Ströme
      statt der 3 aus dem Handoff-Modell
- [x] Geklärt: KEIN Voll-Snapshot — Replay aller Deltas seit 2011-08-19
      (414 GB gz, 38.178 Dateien); Volumen beziffert
- [x] Exakte Postgres-Spalten abgeleitet und in ARCHITECTURE.md §5.2
      eingetragen (alle 7 Zieltabellen + import_state)
- [x] Bootstrap-Strategie als DECISIONS-Eintrag; Zeitabschätzung bewusst
      auf den Probelauf in Phase 8 verlagert — nirgends existiert eine
      belegte E2E-Importdauer (Betreiber-Entscheid 2026-07-25)

Definition of Done: erfüllt 2026-07-25 — Schema in ARCHITECTURE §5,
Bootstrap-/Import-Entscheide in DECISIONS, 9 Fixture-Dateien (8,9 MB,
Tag 2026-07-22 komplett + Edge Cases) unter
tests/fixtures/acoustid-dumps/; Handoff-§11.1/§11.2 geschlossen.

## Phase 1: Recherche — acoustid-index, Upstream-Submit, MB-Schema

Ziel: Restliche offene Punkte aus Handoff §11 schließen.

Aufgaben:
- [x] acoustid-index vollständig ausgeleuchtet: Zig-`main` via GHCR
      (Digest-Pin), API inkl. msgpack, fsync-Oplog/Crash-Verhalten,
      Kennzahlen 40–55 GB + „muss in den RAM" (ARCHITECTURE §5.3,
      docs/research/phase1-acoustid-index.md)
- [x] Index-Platzierung entschieden: SSD-Cache-Pool (DECISIONS)
- [x] Rescoring entschieden: zweistufig, Python-Nachbau `compare2` +
      `extract_query` mit CI-Bit-Verifikation gegen die
      Original-Extension (nur Test-Container); `index.query_hashes`
      konfigurierbar (DECISIONS)
- [x] Upstream-Submit + kompletter API-Vertrag dokumentiert inkl.
      Handoff-Korrektur `/v2/submission_status`, Fehlercode-Tabelle,
      Client-Verhalten Picard/beets (ARCHITECTURE §7,
      docs/research/phase1-api-formate.md)
- [x] MB-Schema: 17 Tabellen, 10-Funktionen-Query-Schicht,
      Redirect-Auflösung, RO-Rolle, Schema-Guard (ARCHITECTURE §5.4,
      docs/research/phase1-mb-schema.md)
- [x] Ergebnisse in ARCHITECTURE.md und DECISIONS.md eingetragen

Definition of Done: erfüllt 2026-07-25 — Handoff-§11.3–§11.5
geschlossen; alle Entscheidungen protokolliert (7 neue
DECISIONS-Einträge).

## Phase 2: Repo-Grundgerüst & CI

Ziel: Baubare, leere Projektstruktur mit grüner CI.

Aufgaben:
- [x] git init; .gitignore (inkl. Fixtures — Lizenzentscheid — und
      .env/config.yaml); Filestruktur gemäß ARCHITECTURE.md §10
- [x] Python-Paketstruktur als uv-Workspace (Python 3.14, ruff,
      pytest; Import-Namen `shared`/`acoustid_api`/`acoustid_importer`/
      `acoustid_watchdog`, siehe DECISIONS)
- [x] .env.example (AOFF_-Bootstrap-Startsatz) und README-Stub mit
      Lizenzhinweisen (Code MIT, Daten CC BY-SA 3.0)
- [x] .github/workflows/ci.yml: Lint + Format-Check + Tests (uv)
- [x] GitHub-Repo angelegt und verbunden:
      https://github.com/shares92/acoustid-offline (öffentlich)
- [x] Steuerungsdateien + docs/ im Repo
- [x] fetch_fixtures.py für reproduzierbare Fixture-Beschaffung

Definition of Done: erfüllt 2026-07-25 — CI auf GitHub grün
(Lint + Format + 31 Tests; Fix nötig: setup-uv-Tag v9 → v9.0.0);
Struktur entspricht §10 (dokumentierte Abweichung: Import-Namen).

## Phase 3: Shared-Paket — Config, Modelle, Logging

Ziel: Gemeinsames Fundament für alle drei Services.

Aufgaben:
- [x] Config-Schema mit allen §6-Schlüsseln inkl. Projekt-Ergänzungen
      (pydantic v2, Enums, HH:MM-Validierung, Secrets maskiert,
      unbekannte Schlüssel Warnung+ignorieren, atomares Schreiben 0600)
- [x] AOFF_-Env-Bootstrap (`EnvSettings.from_env`, testbar ohne
      Prozessumgebung; neu: AOFF_LOG_LEVEL; Test hält .env.example und
      Schema deckungsgleich)
- [x] Strukturiertes JSON-Logging (`setup_logging`, idempotent) +
      Modelle (AuthMode, SubmitMode, SubmissionStatus, StackState mit
      deutschen display_names)
- [x] 123 neue Unit-Tests (Schema, IO, Env, Logging, Modelle)

Definition of Done: erfüllt 2026-07-25 — 153 Tests grün (lokal + CI);
alle §6-Schlüssel abgedeckt. Commit 481315c.

## Phase 4: DB-Schema & Migrationen

Ziel: AcoustID-Postgres steht per Compose mit vollständigem Schema.

Aufgaben:
- [x] Migrationen für alle 7 Dump-Zieltabellen + `import_state` exakt
      nach §5.2 (`local_submission` bewusst erst in Phase 11 — §5.2
      „Weitere Tabellen" ersetzt den alten Aufgabentext)
- [x] Eigener Migrations-Runner (`shared/shared/db/`, kein Alembic):
      idempotent, je Migration eine Transaktion, Advisory-Lock,
      Drift-Erkennung per Checksumme, CLI `python -m shared.db`;
      zwei Gruppen `core`/`indexes` für den Bootstrap-Weg
- [x] docker-compose.yml: db-Service `acoustid-db` (postgres:18,
      Healthcheck, benanntes Volume, kein Host-Publish)
- [x] 33 Integrationstests (Schema-Introspektion inkl. Partialindizes
      und lz4, Idempotenz, Gruppen, Drift) — lokal gegen echtes PG 18
      gelaufen und als CI-Job mit Postgres-Service verankert

Definition of Done: erfüllt 2026-07-25 — Migrationstests lokal und in
CI grün; ein Test hält ARCHITECTURE-§5.2-DDL und Migrations-SQL
anweisungsgleich. Commit 1f294c4.

## Phase 5: Index-Client

Ziel: acoustid-index läuft per Compose und ist aus Python ansprechbar.

Aufgaben:
- [x] compose: index-Service per Digest gepinnt (main @ 2025-10-27),
      UID 6081, kein ports:, wget-Spider-Healthcheck auf
      `/<name>/_health` (start_period 900 s), Volume-Hinweis Cache-Pool
- [x] shared-Client `shared/shared/fpindex/` (query/wire/client/errors,
      msgpack-Kurzform auch für Requests, client-seitige Validierung
      statt stiller Server-Deckelung, eigene Fehlerhierarchie);
      `extract_query` als pure Funktion; neu: `AOFF_INDEX_NAME`
- [x] 145 neue Tests; 18 Integrationstests mit echten
      Fixture-Vektoren gegen das echte Image (lokal + CI)

Definition of Done: erfüllt 2026-07-25 — Integrationstests lokal und
in CI grün; 12 empirische Befunde als Addendum im Forschungsbericht.
Commit 620aa5a. Wichtig für Phase 7/9: Dienst wird erst nach
`ensure_index()` healthy → importer hängt mit `service_started`,
api mit `service_healthy` ab.

## Phase 6: Importer — Download & Parser

Ziel: Delta-Dateien werden korrekt geholt und vollständig geparst
(noch ohne DB-Schreiben).

Aufgaben:
- [ ] Downloader: Sequenzlogik ab `import_state`, Netz-/Retry-Handling
- [ ] Parser für alle Tabellen des Dump-Formats (Phase 0), inkl.
      Fehlerfälle (kaputte Zeile, unbekanntes Feld)
- [ ] Test-Fixtures aus echten Beispiel-Deltas (Phase 0) eingebunden

Definition of Done: Parser-Unit-Tests mit echten Beispiel-Deltas grün.

## Phase 7: Importer — Transaktionaler DB-Import & Index-Feed

Ziel: Deltas landen transaktional in Postgres und im Index; Import ist
resumierbar (Invarianten §8.3/§8.4).

Aufgaben:
- [ ] Import: eine Delta-Datei = eine Transaktion inkl.
      `import_state`-Fortschreibung
- [ ] Resume nach Abbruch/Fehler (nächster Lauf setzt korrekt fort)
- [ ] Index-Feed für neue Fingerprints über den Index-Client
      (aufsteigend nach `fingerprint.id` — ~15 % kleinerer Index;
      Batches à 1000)
- [ ] Integrationstests: Deltas → DB + Index; Abbruch mitten im Lauf →
      Resume korrekt

Definition of Done: Integrationstests inkl. Resume-Fall grün.

## Phase 8: Importer — Bootstrap, Platz-Guard & One-Shot-Job

Ziel: Erst-Import gemäß Phase-0-Strategie; Importer verhält sich als
sauberer One-Shot-Job.

Aufgaben:
- [ ] Bootstrap-Pfad gemäß Phase-0-Entscheidung: Voll-Replay ab
      2011-08-19, alle 7 Ströme, Bulk-Modus (Indizes/FKs nachziehen,
      unsichere PG-Einstellungen nur währenddessen), Download-Prefetch
      entkoppelt vom Import
- [ ] Probelauf-Modus (begrenzter Zeitraum, z. B. `--end-date`) mit
      Messung von Dauer, DB- und Index-Größe + Hochrechnung für die
      Bootstrap-Doku (Betreiber-Entscheid 2026-07-25)
- [ ] Plattenplatz-Guard: freier Platz ≥ `update.min_free_gb`, sonst
      Abbruch mit klarem Ergebnis (Invariante §8.8)
- [ ] One-Shot-Verhalten: definierte Exit-Codes + maschinenlesbares
      Ergebnis-Reporting (Dateien/Zeilen/Fehler) für `update_run`
- [ ] compose: importer-Service mit Profil `job`

Definition of Done: Bootstrap- und Guard-Tests grün; Exit-Codes und
Report-Format dokumentiert; Probelauf am echten Datenbestand
durchgeführt und Zeit-/Größen-Hochrechnung in der Bootstrap-Doku.

## Phase 9: API — /v2/lookup Kern

Ziel: Kompatibler Lookup ohne `meta` gegen lokale Daten
(Vertrag: ARCHITECTURE §7 + docs/research/phase1-api-formate.md).

Aufgaben:
- [ ] FastAPI-App api/: `GET/POST /v2/lookup` (`client`, `fingerprint`,
      `duration`, `maxdurationdiff`, Original-Batchprotokoll
      `fingerprint.N`+`batch`) inkl. gzip-Bodys,
      Chromaprint-Base64-Decoder, Limits (20/100, 1 MiB→413)
- [ ] Match-Pipeline zweistufig: Index-Kandidaten → Python-Rescoring
      (`compare2`-Nachbau, Längenfilter ±7, Cutoff >0,4, max 10,
      dedupliziert) → `track`/`track_mbid`
- [ ] CI-Bit-Verifikation: `extract_query` + `compare2` gegen
      Original-Extension im Test-Container (DECISIONS 2026-07-25)
- [ ] Antwort- und Fehlerformat AcoustID-kompatibel (19 Codes,
      HTTP-Mapping, CORS)
- [ ] compose: api-Service; Integrationstest gegen db + index mit
      Fixtures

Definition of Done: Fixture-Lookup liefert format-kompatible Antwort
(gegen Original-Beispielantworten verglichen); Fehlerfälle im
AcoustID-Fehlerformat; Tests grün.

## Phase 10: API — MB-Resolver & meta

Ziel: Metadaten aus der lokalen MB-Postgres, mit degradiertem Betrieb.

Aufgaben:
- [ ] Gekapselte Read-only-Query-Schicht (Tabellen aus Phase 1),
      `mb.dsn`-Anbindung
- [ ] `meta`-Parameter gemäß Original (u. a. `recordings`,
      `releasegroups`, `compress`)
- [ ] Degradierter Betrieb bei MB-Ausfall: UUIDs + MBIDs ohne Metadaten,
      kein Fehler, Ereignis geloggt (Invariante §8.7)
- [ ] Tests mit MB-Testdaten (Mini-Schema-Fixture) + Ausfall-Test

Definition of Done: meta-Lookup liefert Metadaten; MB-down-Test grün.

## Phase 11: API — /v2/submit (off/local)

Ziel: Kompatibler Submit mit lokaler Speicherung und Indexierung.

Aufgaben:
- [ ] Parameter-Parsing gemäß Original (`client`, `user`,
      `fingerprint.N`, `duration.N`, MBID/Metadaten-Felder)
- [ ] Modi `off`/`local`; `local_submission` mit Statusmaschine
      `new` → `indexed`; Indexierung über Index-Client
- [ ] Kompatibles Antwortformat
- [ ] Tests: Submit → Lookup findet die Submission; Modus `off`

Definition of Done: Tests grün; Statusübergänge korrekt persistiert.

## Phase 12: API — Upstream-Forwarding & Queue

Ziel: Modus `local+upstream` inkl. robuster Fehler-Queue.

Aufgaben:
- [ ] Weiterleitung an api.acoustid.org mit `upstream_app_key`
      (Format aus Phase 1)
- [ ] Statuspfade `forwarded`/`forward_failed`; Retry beim nächsten
      Update-Lauf; nach 7 Fehlversuchen Ereignis für Notification +
      manueller Retry-Hook (Invariante §8.9)
- [ ] Tests mit Mock-Upstream: Erfolg, Fehler, Retry, 7-Fehler-Grenze

Definition of Done: Alle Pfade getestet; Queue-Verhalten dokumentiert.

## Phase 13: API — /v2/lookup/batch & /v2/submission_status

Ziel: Viele Fingerprints in einer Anfrage — ein Weckvorgang; plus der
kleine Original-Status-Endpoint.

Aufgaben:
- [ ] `POST /v2/lookup/batch`: JSON-Array `{fingerprint, duration,
      meta}`, Antwort-Array in gleicher Reihenfolge
- [ ] Limit 100 Einträge/Request mit sauberem Fehler bei Überschreitung
- [ ] `GET/POST /v2/submission_status` (Mehrfach-`id`; unbekannte IDs
      `"pending"`, nie 404) — Handoff-Korrektur, DECISIONS 2026-07-25
- [ ] Tests inkl. Teilfehlern einzelner Einträge

Definition of Done: Batch- und Status-Tests grün; Verhalten
dokumentiert.

## Phase 14: Wächter — Grundgerüst, SQLite & /status

Ziel: Dauerläufer-Skeleton mit Zustandshaltung; `/status` weckt nie.

Aufgaben:
- [ ] FastAPI-App watchdog/; SQLite-Schema: `api_key`, `admin_user`,
      `update_run`, `event_log` (Ringpuffer-Begrenzung)
- [ ] config.yaml lesen/schreiben über shared; Reload-Signal-Mechanik
      Richtung API vorbereiten
- [ ] `GET /status`: Stack-Zustand, Datenstand, letzter Update-Lauf,
      Version — ausschließlich aus Wächter-Daten
- [ ] Erststart: Admin-Passwort generieren (argon2-Hash) + ins Log
- [ ] compose: docker-compose.watchdog.yml

Definition of Done: /status- und Erststart-Tests grün; Event-Log
begrenzt korrekt.

## Phase 15: Wächter — Proxy, Docker-Steuerung & Wecken

Ziel: Kernstück On-Demand-Betrieb: Anfrage weckt den Stack.

Aufgaben:
- [ ] Reverse-Proxy `/v2/*` → acoustid-api
- [ ] Docker-Steuerung über /var/run/docker.sock: Stack-Container
      starten/stoppen — bewusst minimaler Code
- [ ] Wake-on-request: Anfrage halten bis Stack bereit; nach
      `wake.hold_timeout_s` → `503` + `Retry-After`
- [ ] Compose-E2E-Test: Anfrage bei schlafendem Stack → Wecken →
      Antwort

Definition of Done: E2E-Wecktest grün; docker.sock-Codepfad minimal und
isoliert.

## Phase 16: Wächter — Zustandsmaschine, Idle-Stopp & Startfehler

Ziel: Vollständiger Lebenszyklus schlafend → startet → bereit →
stoppt (+ fehler).

Aufgaben:
- [ ] Zustandsmaschine mit den fünf Zuständen (Basis für /status + UI)
- [ ] Idle-Stopp: `idle.timeout_min`; nur ohne API-Anfragen UND ohne
      laufenden Import-/Backup-Job (Invariante §8.5)
- [ ] Stack-Start-Fehler: `503` + Fehlertext, Ereignis geloggt
      (Notification folgt Phase 20)
- [ ] Tests: Idle-Stopp, Stopp-Blockade bei laufendem Job, Fehlerpfad

Definition of Done: Zustandsübergänge vollständig getestet.

## Phase 17: Wächter — Lookup-Cache

Ziel: Cache-Hits wecken das Array nie.

Aufgaben:
- [ ] Cache auf SSD: Schlüssel = Hash(Fingerprint+Duration+
      meta-Parameter), Wert = serialisierte Antwort
- [ ] `cache.enabled`, `cache.max_size_mb` mit Verdrängung
- [ ] Vollständige Invalidierung nach erfolgreichem Delta-Import und
      nach jeder lokalen Submission (Invariante §8.6)
- [ ] Tests: Hit ohne Stack-Start; Invalidierung beider Auslöser

Definition of Done: Cache-Tests grün inkl. Größenbegrenzung.

## Phase 18: Wächter — Auth & Rate-Limit

Ziel: `apikey`-Modus und IP-Rate-Limit am Proxy. Durchsetzungsort
Wächter ist entschieden (DECISIONS 2026-07-25).

Aufgaben:
- [ ] `apikey`-Modus: `client`-Prüfung gegen `api_key`-Tabelle
      (Hash-Vergleich), Fehlerantwort im AcoustID-Fehlerformat,
      „zuletzt benutzt" aktualisieren
- [ ] Whitelist-Schalter `auth.allow_known_client_keys` (Picard/beets,
      default aus — DECISIONS 2026-07-25)
- [ ] Modus `none`: `client` akzeptiert und ignoriert
- [ ] Rate-Limit pro Client-IP (`ratelimit.per_ip_per_min`), aktiv auch
      im Modus `none` → `429` + `Retry-After`
- [ ] Tests: beide Modi, Limit, Cache-Hit-Pfad mit Auth

Definition of Done: Auth-/Limit-Tests grün, auch in Kombination mit
Cache-Hits.

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

Definition of Done: Zyklus-Test grün; Historie korrekt; Stack schläft
nach dem Lauf wieder.

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

Voraussetzung: Design aus der Claude-Design-Session liegt vor.

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

## Phase 29: Release, README & Unraid-Template

Ziel: Veröffentlichbares Gesamtpaket.

Aufgaben:
- [ ] release.yml: drei Images (watchdog, api, importer) → GHCR mit
      einem gemeinsamen Release-Tag (Invariante §8.11)
- [ ] README final: Unraid-Setup (Array/Cache-Zuordnung), generisches
      Docker-Setup, Bootstrap-Anleitung mit realistischer Zeitangabe
      (aus Phase 0/8), Restore, Lizenzhinweis, TLS/apikey-Hinweis bei
      Exponierung
- [ ] Unraid-Community-App-Template (XML) im Repo
- [ ] .env.example final abgleichen

Definition of Done: Tag-Release baut drei Images mit identischem Tag;
Template validiert; README vollständig; Erfolgskriterien aus
ARCHITECTURE.md §1 nachweisbar erfüllt.
