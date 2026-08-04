# HANDOFF.md — acoustid-offline (v1, ARCHIV)

**Ersetzt durch v2 (docs/HANDOFF.md, „musicmeta-offline", 03.08.2026)
gemäß dortigem §16-Migrationsanhang; archiviert am 2026-08-04 (M0).**

Spezifikations-Input für die Implementierungs-Session in Claude Code.
Aus diesem Dokument werden dort die Steuerungsdateien (ARCHITECTURE.md,
PROGRESS.md, DECISIONS.md, LEARNINGS.md) generiert und phasenweise umgesetzt.
Implementierung erst nach explizitem Go des Auftraggebers.

Stand: 25.07.2026 — Ergebnis der Architektur-Session.

---

## 1. Projektziel

Eine selbst gehostete, offline-fähige AcoustID-Instanz als Docker-Stack:
Audio-Fingerprint-Lookup (Chromaprint → AcoustID-UUID → MusicBrainz-Recording
inkl. Metadaten) ohne Abhängigkeit vom öffentlichen api.acoustid.org.

**Erfolgskriterien:**
1. Ein Standard-Client (primär DroppedNeedle, außerdem Picard/beets per
   URL-Umbiegung) bekommt auf `/v2/lookup` korrekte, API-kompatible Antworten.
2. Der Stack schläft im Normalzustand vollständig (Array-Platten dürfen
   herunterfahren); nur der Wächter läuft dauerhaft auf dem Cache.
3. Der Datenbestand aktualisiert sich täglich automatisch per Delta-Import,
   inkl. Selbst-Wecken und Wieder-Einschlafen.
4. Läuft auf jedem Docker-Host; Referenz-Deployment ist Unraid
   (DB/Index auf dem Array, Wächter auf dem Cache-Pool).

## 2. Kontext & Constraints

- **Host:** Unraid-Server; Postgres + Index auf dem Array (Spindeln),
  Wächter + Cache-Daten auf dem SSD-Cache-Pool. Cache ist zu klein für den
  Datenbestand (dreistellige GB-Größe erwartet und akzeptiert).
- **Primärer Client:** DroppedNeedle (eigene App des Auftraggebers);
  API-Kompatibilität zu api.acoustid.org ist Pflicht, damit auch
  Drittclients funktionieren.
- **MusicBrainz:** Lokaler Spiegel vorhanden (musicbrainz-docker-Stack mit
  eigener Postgres). Direkter Read-only-DB-Zugriff ist möglich und wird
  genutzt.
- **Datenquelle:** Öffentliche AcoustID-Datenbank. Wird täglich als
  inkrementelle JSON-Update-Dateien veröffentlicht. Lizenz CC BY-SA 3.0,
  MusicBrainz-AcoustID-Mapping Public Domain.
- **Netz:** Betrieb primär im LAN/VPN. Exponierung nach außen möglich,
  dann zwingend `apikey`-Modus + Reverse-Proxy mit TLS (Doku-Hinweis).
- **Repo:** Öffentlich auf GitHub. GitHub Actions bauen alle Images nach
  GHCR (ghcr.io), gemeinsamer Release-Tag für alle Images pro Release.

## 3. Architektur-Überblick

Ein Repo, fünf Container, zwei Compose-Dateien:

**Immer an (Cache-Pool):**
| Container | Image | Aufgabe |
|---|---|---|
| `acoustid-watchdog` | eigenes Image | Reverse-Proxy mit Weck-Logik, Scheduler, Admin-UI, Lookup-Cache, Notifications, Metrics |

**Stack, schlafend (Array):**
| Container | Image | Aufgabe |
|---|---|---|
| `acoustid-api` | eigenes Image | `/v2/lookup`, `/v2/submit`, Batch-Endpoint, MB-Metadaten-Auflösung |
| `acoustid-importer` | eigenes Image | One-Shot-Job: Delta-Download, Import, Index-Feed, Backup |
| `acoustid-db` | offizielles Postgres-Image (neueste stabile Major) | AcoustID-Datenbestand + lokale Submissions |
| `acoustid-index` | offizielles acoustid-index-Image | Fingerprint-Suchindex (Matching-Kern) |

**Datenflüsse:**
- Client → Wächter (Proxy) → API → Index (Match) + eigene Postgres
  (Mappings) + MB-Postgres (Metadaten, read-only).
- Scheduler (Wächter) → weckt Stack → startet Importer-Job → Deltas
  einspielen → Stack schläft wieder.
- Admin-UI läuft vollständig im Wächter; Aktionen, die den Stack
  brauchen, zeigen den Schlafzustand und bieten einen Weck-Button.

**Grundsatzentscheidungen (mit Begründung):**
- Eigener schlanker API-Layer statt offiziellem acoustid-server: weniger
  Ballast; Dump-Import, MB-Direktanbindung und Modi-Schalter sind ohnehin
  Sonderwege. acoustid-index bleibt als erprobter Suchkern gesetzt.
- MB-Anbindung per direkter Read-only-Query aus dem API-Service (kein
  Foreign Data Wrapper): entkoppelter, einfacher zu debuggen.
- Wächter steuert den Stack über `/var/run/docker.sock` (bewusst
  akzeptiertes Risiko im LAN; deshalb minimaler Code im Wächter).
- Getrennte Images für Wächter/API/Importer: minimale Angriffsfläche im
  docker.sock-Container, Update-Entkopplung (nur Wächter-Neustarts sind
  spürbar), kleiner Dauerläufer auf dem Cache. Versionskonsistenz über
  gemeinsamen Release-Tag aus einem Actions-Workflow.
- Config/Keys/Logs liegen beim Wächter auf dem Cache (nicht in der
  Array-Postgres), damit die Admin-UI bei schlafendem Stack voll
  funktionsfähig ist.

## 4. Technologie-Stack

Immer neueste stabile Version zum Implementierungszeitpunkt:

- **Sprache:** Python (API-Layer, Importer, Wächter — eine Sprache für alles)
- **Web-Framework:** FastAPI (API + Admin-UI-Routen)
- **UI:** Server-rendered — Jinja2-Templates + HTMX, kein Frontend-Build
- **Datenbanken:** PostgreSQL (AcoustID-Daten), SQLite (Wächter-Zustand)
- **Suchindex:** acoustid-index (offizielles Image)
- **Deployment:** Docker Compose (zwei Dateien), Images via GHCR
- **CI:** GitHub Actions (Build, Tests, Multi-Image-Release mit einem Tag)

## 5. Funktionsumfang

### Enthalten
1. **Lookup:** `/v2/lookup` API-kompatibel zu api.acoustid.org
   (Fingerprint + Duration → AcoustID-UUIDs, Scores, optional `meta`
   mit Recording-Metadaten aus der lokalen MB-Postgres).
2. **Submit:** `/v2/submit` API-kompatibel. Drei Modi (einstellbar):
   `off` / `local` (nur lokale Speicherung + Indexierung) /
   `local+upstream` (zusätzlich Weiterleitung an api.acoustid.org mit
   dortigem Application-Key).
3. **Batch-Lookup:** Eigener Endpoint, nimmt viele Fingerprints in einer
   Anfrage an — ein Weckvorgang statt vieler Einzelanfragen.
4. **Readiness:** `/status` im Wächter, weckt nie das Array; liefert
   Stack-Zustand (schlafend/startend/bereit/Fehler), Datenstand,
   letzte Update-Zeit.
5. **Lookup-Cache:** Ergebnis-Cache im Wächter (Fingerprint-Hash →
   Antwort) auf SSD; Cache-Hits wecken das Array nicht.
   Invalidierung nach jedem erfolgreichen Delta-Import.
6. **On-Demand-Betrieb:** Wächter weckt den Stack bei eingehender
   Anfrage, hält die Anfrage bis der Stack bereit ist; Auto-Stopp nach
   Idle-Timeout.
7. **Täglicher Delta-Import:** Scheduler im Wächter weckt den Stack,
   startet den Importer-Job, spielt alle neuen JSON-Deltas ein,
   invalidiert den Cache, legt den Stack wieder schlafen.
8. **Admin-UI:** Konfiguration bearbeiten, API-Key-Verwaltung,
   manuelles Update/Start/Stopp, Logs + Statistiken. Immer mit
   Passwort-Login. Details siehe DESIGN_HANDOFF.md.
9. **Auth für die API:** Modi `none` / `apikey` (einstellbar,
   Key-Verwaltung in der Admin-UI).
10. **Benachrichtigungen:** ntfy/Webhook UND E-Mail (SMTP), beide
    einzeln einstellbar; Ereignisse: Import fehlgeschlagen, Plattenplatz
    knapp, Stack-Start-Fehler, Upstream-Submit dauerhaft fehlgeschlagen.
11. **Backup lokaler Unikate:** Zeitgesteuerter Job sichert nur
    `local_submission`-Daten und die Wächter-SQLite in ein
    konfigurierbares Backup-Verzeichnis (der öffentliche Bestand ist
    jederzeit aus den Dumps rekonstruierbar).
12. **Prometheus-Metrics:** Optionaler `/metrics`-Endpoint im Wächter
    (default aus).
13. **Rate-Limiting:** Einfaches Limit pro Client-IP, aktiv auch im
    Modus `none` (Werte einstellbar).
14. **Unraid-Community-App-Template:** XML-Template im Repo für
    Ein-Klick-Installation.

### Bewusst ausgeschlossen
- Kein eigener MusicBrainz-Spiegel (wird als extern vorhanden
  vorausgesetzt; bei Nichterreichbarkeit degradierter Betrieb).
- Kein Fingerprint-Berechnen serverseitig (Chromaprint läuft im Client).
- Keine Volltext-/Metadaten-Suche, kein Browsing des Datenbestands —
  reine Fingerprint-Auflösung.
- Keine Mehrbenutzer-/Rollenverwaltung in der Admin-UI (ein Admin-Login).
- Kein Kubernetes/Helm — Docker Compose only.
- Keine Weiterverteilung des Datenbestands (Lizenzthema bleibt beim
  Betreiber).

## 6. Konfiguration — Konstanten, Defaults, feste Werte

Alle Laufzeit-Einstellungen leben in `config.yaml` auf dem Cache-Volume
des Wächters und sind über die Admin-UI editierbar. Env-Variablen
(Prefix `AOFF_`) nur für Bootstrap (Pfade, Ports, DB-Zugänge).

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `auth.mode` | `none` | `none` \| `apikey` |
| `submit.mode` | `local` | `off` \| `local` \| `local+upstream` |
| `submit.upstream_app_key` | leer | Application-Key für api.acoustid.org |
| `wake.hold_timeout_s` | `90` | Max. Haltezeit einer Anfrage beim Wecken |
| `idle.timeout_min` | `15` | Auto-Stopp nach Inaktivität |
| `update.time` | `04:00` | Täglicher Delta-Import (lokale Zeit) |
| `update.min_free_gb` | `50` | Mindest-Plattenreserve vor Import |
| `cache.enabled` | `true` | Lookup-Cache an/aus |
| `cache.max_size_mb` | `512` | Obergrenze Lookup-Cache |
| `ratelimit.per_ip_per_min` | `120` | Anfragen pro IP pro Minute |
| `metrics.enabled` | `false` | Prometheus-Endpoint |
| `notify.ntfy.url` | leer | ntfy/Webhook-Ziel (leer = aus) |
| `notify.smtp.*` | leer | Host, Port, User, Pass, From, To (leer = aus) |
| `backup.dir` | leer | Backup-Ziel (leer = Backup aus) |
| `backup.time` | `04:45` | Backup nach dem Update-Lauf |
| `mb.dsn` | leer | Read-only-DSN der MusicBrainz-Postgres |

Feste Werte:
- **Ports:** Wächter lauscht auf einem Port (default `8080`) für API-Proxy
  und Admin-UI unter `/admin`. Port per Env änderbar.
- **Container-Namen:** `acoustid-watchdog`, `acoustid-api`,
  `acoustid-importer`, `acoustid-db`, `acoustid-index`.
- **Idle-Definition:** keine API-Anfrage im Timeout-Fenster UND kein
  laufender Import/Backup-Job.
- **Admin-Login:** ein Benutzer, Passwort-Hash (argon2) in der SQLite;
  Erst-Passwort beim ersten Start generiert und geloggt.

## 7. Datenmodell

Exakte Spaltendefinitionen der AcoustID-Tabellen ergeben sich aus dem
JSON-Dump-Format (Recherchepunkt, siehe §11). Logisches Modell:

### PostgreSQL (Stack, Array)
- **`track`** — AcoustID-UUID; aus den Dumps.
- **`fingerprint`** — Fingerprint-Metadaten (Länge, Track-Zuordnung,
  Submission-Zähler) + Verweis auf den Index-Eintrag. Die
  Fingerprint-Vektoren selbst leben im acoustid-index.
- **`track_mbid`** — Zuordnung AcoustID ↔ MusicBrainz-Recording
  (inkl. Disabled-Flag, Quelle).
- **`local_submission`** — eigene Einreichungen: Fingerprint-Daten,
  Metadaten aus dem Submit, Zeitstempel, Status
  (`new` → `indexed` → `forwarded` | `forward_failed`).
- **`import_state`** — zuletzt erfolgreich eingespielte Delta-Datei
  (Dateiname/Sequenznummer, Zeitstempel, Zeilenzähler) für
  resumierbaren Import.

### SQLite (Wächter, Cache)
- **`api_key`** — Key (Hash), Label, aktiv, erstellt, zuletzt benutzt.
- **`admin_user`** — Login, Passwort-Hash.
- **`update_run`** — Historie der Import-/Backup-Läufe: Start, Ende,
  eingespielte Dateien, Zeilen, Ergebnis, Fehlermeldung.
- **`event_log`** — Ereignisse (Start/Stopp, Wecken, Fehler,
  Notifications) mit Level und Zeitstempel, ringpuffer-artig begrenzt.
- **Lookup-Cache** — eigene Tabelle oder Dateicache; Schlüssel =
  Hash(Fingerprint+Duration+meta-Parameter), Wert = serialisierte
  Antwort, invalidiert nach Delta-Import.

### config.yaml (Wächter, Cache)
Siehe §6. Wird vom Wächter gelesen/geschrieben; API-Layer erhält die
relevante Teilmenge beim Start bzw. Reload-Signal vom Wächter.

## 8. API-Spezifikation

### Kompatibel zu api.acoustid.org
- **`GET/POST /v2/lookup`** — Parameter `client`, `fingerprint`,
  `duration`, `meta` (u. a. `recordings`, `releasegroups`, `compress`
  gemäß Original). Antwortformat identisch zum Original (JSON,
  `status`, `results[]` mit `id`, `score`, optional `recordings[]`).
  Im Modus `apikey` wird `client` gegen die Key-Liste geprüft; im Modus
  `none` wird `client` ignoriert, aber akzeptiert.
- **`POST /v2/submit`** — Parameter gemäß Original (`client`, `user`,
  `fingerprint.N`, `duration.N`, MBID/Metadaten-Felder). Verhalten je
  nach `submit.mode`; Antwortformat identisch zum Original.

### Eigene Endpoints
- **`POST /v2/lookup/batch`** — JSON-Body mit Array von
  `{fingerprint, duration, meta}`; Antwort: Array in gleicher
  Reihenfolge. Obergrenze pro Request (fest: 100 Einträge).
- **`GET /status`** — Wächter-Endpoint, weckt nie: Stack-Zustand,
  Datenstand (letzte Delta-Sequenz), letzter Update-Lauf, Version.
- **`GET /metrics`** — Prometheus-Format, nur wenn aktiviert.
- **`/admin/...`** — Admin-UI (server-rendered), Passwort-geschützt.

### Fehlerverhalten der API
- Aufwecken: Anfrage wird gehalten; erst nach `wake.hold_timeout_s`
  ohne Bereitschaft → `503` mit `Retry-After`.
- Stack-Start-Fehler → `503` + Fehlertext + Notification.
- Ungültiger/fehlender Key im `apikey`-Modus → Fehlerantwort im
  AcoustID-Fehlerformat.
- Rate-Limit überschritten → `429` + `Retry-After`.

## 9. Verhaltensregeln & Invarianten

1. **Der Wächter weckt, sonst niemand.** Nur der Wächter startet/stoppt
   Stack-Container (docker.sock). API/Importer steuern nie Docker.
2. **Kein UI-Aufruf weckt das Array.** Admin-UI und `/status` arbeiten
   ausschließlich mit Wächter-Daten; Array-Aktionen nur nach explizitem
   Weck-Button bzw. eingehender API-Anfrage.
3. **Stale statt Sperre.** Während des Delta-Imports werden Lookups aus
   dem alten Bestand weiterbedient; der Import ist transaktional
   (Delta-Datei = eine Transaktion). Kein Wartungsfenster.
4. **Resumierbarer Import.** Nach Abbruch/Fehler bleibt `import_state`
   auf der letzten vollständig eingespielten Datei; nächster Lauf setzt
   dort fort. Ein fehlgeschlagener Lauf wird beim nächsten Zyklus
   automatisch wiederholt.
5. **Idle-Stopp nur im Ruhezustand.** Auto-Stopp nur, wenn im
   Timeout-Fenster keine API-Anfragen liefen UND kein Import-/Backup-Job
   aktiv ist.
6. **Cache-Kohärenz.** Lookup-Cache wird nach jedem erfolgreichen
   Delta-Import und nach jeder lokalen Submission vollständig
   invalidiert.
7. **Degradierter Betrieb bei MB-Ausfall.** Ist die MB-Postgres nicht
   erreichbar, liefert Lookup AcoustID-UUIDs + MBIDs ohne Metadaten
   (kein Fehler); Ereignis wird geloggt.
8. **Plattenplatz-Guard.** Vor jedem Import: freier Platz ≥
   `update.min_free_gb`, sonst Abbruch + Notification.
9. **Upstream-Queue.** Fehlgeschlagene Upstream-Submits bleiben in
   `local_submission` (`forward_failed`) und werden beim nächsten
   Update-Lauf erneut versucht; nach N Fehlversuchen (fest: 7)
   Notification und manueller Retry über die Admin-UI.
10. **Secrets nie im Repo.** Alle Zugänge über `.env`/`config.yaml`;
    `.env.example` dokumentiert alles.
11. **Ein Release = ein Tag = alle Images.** Wächter, API und Importer
    werden immer gemeinsam getaggt und veröffentlicht.

## 10. Filestruktur (Repo)

```
acoustid-offline/
├── docker-compose.yml            # Stack: api, importer(profil: job), db, index
├── docker-compose.watchdog.yml   # Wächter (immer an)
├── .env.example
├── README.md                     # Setup Unraid + generisch, Lizenzhinweis Daten
├── unraid/                       # Community-App-Template (XML)
├── watchdog/
│   ├── Dockerfile
│   └── app/                      # Proxy, Scheduler, Docker-Steuerung,
│       ├── ...                   # Admin-Routen, Auth, Cache, Notify, Metrics
│       ├── templates/            # Jinja2
│       └── static/
├── api/
│   ├── Dockerfile
│   └── app/                      # /v2/lookup, /v2/submit, /v2/lookup/batch,
│                                 # Index-Client, MB-Resolver
├── importer/
│   ├── Dockerfile
│   └── app/                      # Delta-Download, Parser, DB-Import,
│                                 # Index-Feed, Backup-Job
├── shared/                       # Config-Schema, Modelle, Logging (Python-Paket)
├── docs/
│   ├── HANDOFF.md                # dieses Dokument
│   └── DESIGN_HANDOFF.md         # UI-Spezifikation für Claude Design
├── tests/                        # Unit + Integrationstests (Compose-basiert)
└── .github/workflows/            # ci.yml (Tests), release.yml (3 Images → GHCR)
```

## 11. Offene Punkte (Recherche zu Beginn der Implementierung)

1. **JSON-Delta-Format im Detail:** Tabellen-/Feldstruktur der täglichen
   Update-Dateien von data.acoustid.org; daraus die exakten
   Postgres-Spalten ableiten.
2. **Bootstrap-Strategie:** Gibt es einen aktuellen Voll-Snapshot, oder
   müssen alle Deltas seit Beginn nachgespielt werden? Erwartete Dauer
   des Erst-Imports auf Array-Spindeln messen; ggf. Bootstrap-Doku mit
   realistischer Zeitangabe.
3. **acoustid-index:** Aktuelle Version, Fütterungs-/Abfrage-API,
   Speicherbedarf, Verhalten bei Absturz (Rebuild-Kosten), Eignung des
   offiziellen Images.
4. **Upstream-Submit:** Exaktes Format und Application-Key-Registrierung
   bei api.acoustid.org für den `local+upstream`-Modus.
5. **MB-Schema-Version:** Welche Tabellen/Views des
   musicbrainz-docker-Stacks werden für die Metadaten-Auflösung
   benötigt; Absicherung gegen Schema-Änderungen (definierte Query-Schicht).
6. **Auth-Default bestätigt als `none`** (Annahme aus der Session,
   vom Auftraggeber nicht widersprochen).

## 12. Risiken

1. **Dump-Format/Bootstrap (hoch):** Unverifiziertes Format; ohne
   Snapshot potenziell tagelanger Erst-Import. Größtes Projektrisiko —
   zuerst verifizieren, bevor weitere Phasen starten.
2. **acoustid-index-Unbekannte (mittel):** Version/API/Ressourcenbedarf
   ungeprüft; Index-Rebuild nach Absturz kann teuer sein.
3. **Postgres auf Spindeln (mittel):** Random-I/O macht Importe langsam;
   mitigiert durch Stale-Serving und nächtliches Zeitfenster.
4. **MB-Schema-Kopplung (niedrig):** Read-only-Queries können bei
   MB-Updates brechen; mitigiert durch gekapselte Query-Schicht und
   degradierten Betrieb.
5. **docker.sock im Wächter (akzeptiert):** Bewusstes Risiko; mitigiert
   durch minimalen Code, Passwort-Login, Rate-Limit, LAN-Betrieb.
6. **Lizenz CC BY-SA 3.0 (niedrig):** Für Eigenbetrieb unkritisch;
   README weist auf Bedingungen bei Weitergabe hin.

## 13. Deliverables der Implementierung

1. Lauffähiger Stack gemäß §3–§10 inkl. Tests.
2. README mit Unraid- und generischem Docker-Setup, Bootstrap-Anleitung,
   Lizenzhinweis.
3. Unraid-Community-App-Template.
4. GitHub-Actions-Workflows (CI + Release → GHCR, ein Tag für drei Images).
5. Admin-UI gemäß DESIGN_HANDOFF.md (UI-Design entsteht in separater
   Claude-Design-Session auf Basis dieses Handoffs).
