# HANDOFF.md — musicmeta-offline (v2)

Spezifikations-Input für die Implementierung in Claude Code. **v2 ersetzt
den bisherigen HANDOFF vollständig** (Projekt hieß dort acoustid-offline).
Da bereits ein laufendes Claude-Code-Projekt auf Basis von v1 existiert
(Stand: Phase 19), enthält §16 einen verbindlichen Migrationsanhang.
Erster Implementierungsschritt ist die dort beschriebene Impact-Analyse —
nicht blind neu aufsetzen.

Stand: 03.08.2026 — Ergebnis der erweiterten Architektur-Session.

---

## 1. Projektziel

Ein selbst gehostetes Offline-Gateway für Musik-Metadaten in **einem
einzigen Docker-Container**: Audio-Fingerprint-Auflösung (AcoustID),
Release-/Artist-/Label-Daten (Discogs), Frontcover (Cover Art Archive)
und ergänzende Metadaten/Artwork (TheAudioDB) — ohne laufende
Abhängigkeit von den öffentlichen Diensten.

**Erfolgskriterien:**
1. Standard-Clients (primär DroppedNeedle; außerdem Picard/beets für den
   AcoustID-Teil) funktionieren per Umbiegen der Basis-URL.
2. Der Container läuft dauerhaft, aber im Ruhezustand sind alle
   datenhaltenden Prozesse gestoppt und das Array darf schlafen; nur der
   Wächter-Prozess bleibt aktiv (Cache-Volume).
3. Datenbestände aktualisieren sich automatisch: AcoustID täglich
   (Deltas), Discogs monatlich (Dumps), CAA-Cover per Hintergrund-Crawler
   und täglichem Nachzügler-Job.
4. Läuft auf jedem Docker-Host; Referenz-Deployment Unraid (Bilder- und
   DB-Volumes auf dem Array, Container/Wächter-Daten auf dem Cache-Pool).

## 2. Kontext & Constraints

- **Host:** Unraid. Array (Spindeln) für alle großen Datenbestände;
  SSD-Cache-Pool für Container-Image, Wächter-Daten, Lookup-Cache.
- **Speicherbedarf Array:** realistisch **1–2 TB** (siehe §15.1).
- **Primärer Client:** DroppedNeedle (eigene App des Auftraggebers).
- **MusicBrainz:** Lokaler Spiegel vorhanden (musicbrainz-docker-Stack,
  eigene Postgres, direkter Read-only-Zugriff). Liefert Metadaten,
  Cover-Verzeichnis (`cover_art_archive`-Schema) und
  MBID↔Discogs-Zuordnung (URL-Relationships).
- **Externe Zugänge (vom Betreiber beizubringen):** TheAudioDB-API-Key,
  Discogs-Token (für Bilder-API), optional AcoustID-Application-Key
  (Upstream-Submit).
- **Netz:** Primär LAN/VPN. Bei Exponierung zwingend `apikey`-Modus +
  Reverse-Proxy mit TLS (Doku-Hinweis).
- **Repo:** Öffentlich auf GitHub, GitHub Actions bauen **ein Image**
  nach GHCR. Repo-Defaults sind konservativ (CAA-Crawler aus).
- **Lizenzen:** AcoustID-Daten CC BY-SA 3.0 (Mapping Public Domain),
  Discogs-Dumps CC0, Cover/TheAudioDB-Inhalte mit eigenen Rechten —
  keine Weiterverteilung der Bestände, README-Hinweis.

## 3. Architektur — Ein-Container-Modell

Ein Image, ein Container, interner Prozess-Supervisor. **Kein
docker.sock, kein Compose-Stack mit mehreren Diensten.**

**Prozessgruppen im Container:**

| Gruppe | Prozesse | Lebenszyklus |
|---|---|---|
| Immer aktiv | Wächter (Proxy, Scheduler, Admin-UI, Lookup-Cache, Notifications, Metrics) | läuft mit dem Container |
| Schlafend | PostgreSQL, acoustid-index, API-Service | Supervisor startet sie bei Bedarf, stoppt sie nach Idle |
| Jobs | Importer (AcoustID-Delta, Discogs-Dump), CAA-Crawler, Nachzügler, Backup, Queue-Sender | Supervisor startet sie zeit- oder ereignisgesteuert als Subprozesse |

**Volumes:**

| Mount | Ablage | Inhalt |
|---|---|---|
| `/data/db` | Array | PostgreSQL (AcoustID- + Discogs-Schema) |
| `/data/index` | Array | acoustid-index |
| `/data/covers` | Array | Cover-Dateien (Sharding nach MBID-Präfix) |
| `/data/tadb` | Array | TheAudioDB-Cache (JSON + Bilder) |
| `/config` | Cache | config.yaml, SQLite, Lookup-Cache, Logs |
| `/import` | Array | temporäre Dump-Downloads/Staging |

Wichtig fürs Deployment (README): Das Docker-Image selbst und `/config`
müssen auf dem Cache liegen, sonst hält der laufende Container das
Array wach.

**Schlaf-Logik (prozessintern):** "Schlafend" heißt: Postgres, Index und
API-Prozess sind gestoppt; auf den Array-Volumes passiert kein I/O;
Unraid legt die Platten schlafen. Der Wächter beantwortet `/status` und
Admin-UI aus `/config`, ohne das Array zu berühren. Eingehende
API-Anfragen oder fällige Jobs lassen den Supervisor die schlafenden
Prozesse starten (Anfrage wird gehalten, siehe §9).

**Datenflüsse:**
- Lookup: Client → Wächter (Cache-Check) → API-Prozess → Index/Postgres
  → MB-Postgres (read-only) → Antwort.
- Cover: Client → Wächter → lokale Datei; bei Miss on-demand-Kette
  CAA → TheAudioDB → Discogs (nur mit Internet), Ergebnis wird
  gespeichert.
- Updates/Crawls: Scheduler → Supervisor weckt Prozesse → Job läuft →
  Idle → Schlaf.

**Grundsatzentscheidungen (mit Begründung):**
- **Ein Container statt Stack** (Auftraggeber-Entscheidung, Konsequenzen
  bestätigt): Postgres und acoustid-index werden ins eigene Image
  eingebacken und selbst gepflegt; kein docker.sock mehr nötig
  (Sicherheitsgewinn); jedes Update ersetzt den ganzen Container;
  Prozess-Isolation und -Neustarts verantwortet der Supervisor.
- Eigener schlanker API-Layer statt offiziellem acoustid-server
  (unverändert aus v1).
- MB-Anbindung per direkter Read-only-Query (unverändert aus v1).
- Config/Keys/Logs auf dem Cache, nie in der Array-Postgres
  (unverändert aus v1).

## 4. Quellen & Betriebsarten

| Quelle | Betriebsart | Aktualisierung | Offline-Charakter |
|---|---|---|---|
| AcoustID | Dump-Spiegel (JSON-Deltas) | täglich, automatisch | echt offline |
| Discogs-Daten (Releases, Master, Artists, Labels — komplett) | Dump-Spiegel (monatliche XML-Dumps) | monatlich, automatisch (täglicher Verfügbarkeits-Check) | echt offline |
| CAA-Frontcover | **Voll-Spiegel** per Hintergrund-Crawler + Lazy-Fallback | Erst-Crawl einmalig (Wochen), danach täglicher Nachzügler | echt offline nach Crawl-Abschluss |
| Discogs-Bilder | Lazy-Cache (nur als Cover-Fallback) | on-demand | offline nach Erstabruf |
| TheAudioDB | Lazy-Cache (API-Proxy) | on-demand; Cache unbegrenzt, manuell invalidierbar | offline nach Erstabruf |

**Cover-Politik:** Genau **ein Bild pro Release** (Frontcover), maximal
**1200px** lange Kante, als JPEG normalisiert. Beschaffungskette:
CAA (1200-Variante) → TheAudioDB → Discogs. Releases ohne Cover in
allen drei Quellen bekommen einen Negativ-Eintrag mit
Wiederholungsintervall (fest: 30 Tage).

**CAA-Crawler:** Arbeitet die Release-Liste aus dem MB-Spiegel ab
(`cover_art_archive`-Schema als Verzeichnis, nur Releases mit
vorhandenem Front-Artwork). Gedrosselt (Default 2 Anfragen/s,
einstellbar), resumierbar (Cursor in `crawl_state`), abschaltbar
(Repo-Default: aus). Erwartete Erst-Crawl-Dauer bei ~2 Mio. Covern:
2–4 Wochen Dauerbetrieb — solange schläft das Array nicht.
Recherchepunkt §14: offizieller Bulk-Bezugsweg, der den Crawl verkürzt.

## 5. Technologie-Stack

Immer neueste stabile Version zum Implementierungszeitpunkt:

- **Sprache:** Python (alle Eigenkomponenten)
- **Web:** FastAPI; UI server-rendered mit Jinja2 + HTMX (kein Build)
- **Prozess-Supervisor:** s6-overlay oder supervisord — Entscheidung in
  der Impact-Analyse (§16), Kriterium: sauberes Start/Stopp einzelner
  Dienste zur Laufzeit per API/Signal
- **Datenbanken:** PostgreSQL (eingebacken; AcoustID- + Discogs-Schema),
  SQLite (Wächter)
- **Suchindex:** acoustid-index (eingebacken)
- **Bildverarbeitung:** Pillow (Normalisierung auf max. 1200px, JPEG)
- **XML-Streaming:** für Discogs-Dumps (Referenz-Tooling wie
  discogs-load/discogs-xml2db als Schema-Vorlage prüfen)
- **Deployment:** ein Docker-Image, eine Compose-Datei (ein Service),
  GHCR via GitHub Actions

## 6. Funktionsumfang

### Enthalten
1. **AcoustID komplett wie v1:** `/v2/lookup` und `/v2/submit`
   API-kompatibel; Submit-Modi `off`/`local`/`local+upstream`;
   Batch-Endpoint; täglicher Delta-Import; Upstream-Queue.
2. **Discogs-Spiegel:** kompletter Datenbestand aus den Monats-Dumps;
   API-kompatibles GET-Subset (`/discogs/releases/{id}`,
   `/discogs/masters/{id}`, `/discogs/artists/{id}`,
   `/discogs/labels/{id}`); Bilder als Lazy-Cache über die
   authentifizierte Discogs-API (nur innerhalb der Cover-Kette).
3. **CAA:** URL-kompatibler Endpoint
   (`/caa/release/{mbid}/front` → lokale 1200px-Datei);
   Voll-Spiegel-Crawler wie §4; Lazy-Fallback.
4. **TheAudioDB:** transparenter Cache-Proxy unter `/tadb/…`
   (API-kompatibles Pfadschema, eigener Key des Betreibers);
   Cache unbegrenzt, Invalidierung einzeln/gesamt per Admin-UI.
5. **Vereinheitlichte API:**
   - `/v1/identify` — Fingerprint + Duration → Gesamtpaket:
     AcoustID-Match, MB-Metadaten, Discogs-Zuordnung, TheAudioDB-Extras,
     Cover-URL.
   - `/v1/release/{mbid}` — dasselbe Paket per MBID.
   - `/v1/cover/{mbid}` — liefert direkt das Bild (löst bei Miss die
     Kette aus).
6. **On-Demand-Betrieb:** wie v1, jetzt prozessintern (Supervisor);
   Anfrage-Halten beim Wecken, Idle-Auto-Stopp.
7. **Scheduler:** AcoustID täglich 04:00; Discogs-Verfügbarkeits-Check
   täglich 05:00 (Import bei neuem Dump); CAA-Nachzügler täglich nach
   dem AcoustID-Lauf; Backup 04:45; alle Zeiten einstellbar.
8. **Admin-UI** (Passwort-Login, server-rendered): Konfiguration,
   API-Keys, manuelle Jobs, Logs, Statistiken — erweitert um
   Vier-Quellen-Datenstand und Crawler-Steuerung. Details
   DESIGN_HANDOFF.md v2.
9. **Auth-Modi** `none`/`apikey` für die gesamte externe API
   (alle Quellpfade einheitlich).
10. **Lookup-Cache** im Wächter (jetzt quellenübergreifend:
    AcoustID-Antworten, v1-Pakete, TADB-Antworten; Cover sind ohnehin
    Dateien).
11. **Benachrichtigungen** ntfy/Webhook + SMTP (wie v1), erweitert um
    Crawler-Ereignisse (abgeschlossen, festgefahren) und
    Dump-Import-Ergebnisse beider Quellen.
12. **Backup lokaler Unikate:** `local_submission`, Wächter-SQLite,
    `artwork`-Verzeichnis-Metadaten (nicht die Bilder selbst — die sind
    rekonstruierbar; Schalter, um Cover doch einzuschließen).
13. **Prometheus-Metrics** (default aus), **Rate-Limiting** pro
    Client-IP, **Unraid-Community-App-Template** — wie v1.

### Bewusst ausgeschlossen
- Kein eigener MusicBrainz-Spiegel (extern vorausgesetzt; degradierter
  Betrieb bei Ausfall).
- Kein serverseitiges Fingerprinting (Chromaprint im Client).
- Keine Discogs-Schreib-API (nur GET-Subset), keine
  Marketplace-/User-Endpunkte.
- Nur Frontcover — keine Booklets, Rückseiten, Medium-Bilder,
  Künstlerfotos als Spiegel (TheAudioDB-Artwork nur als Cache-Proxy).
- Keine Volltextsuche über die Bestände.
- Keine Mehrbenutzer-/Rollenverwaltung (ein Admin-Login).
- Kein Kubernetes/Helm.
- Keine Weiterverteilung der Datenbestände.

## 7. Konfiguration — Konstanten, Defaults, feste Werte

`config.yaml` auf `/config` (Cache), editierbar über die Admin-UI.
Env-Variablen (Prefix `MMO_`) nur für Bootstrap (Ports, Pfade).

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `auth.mode` | `none` | `none` \| `apikey` |
| `ratelimit.per_ip_per_min` | `120` | externes Rate-Limit |
| `acoustid.submit.mode` | `local` | `off` \| `local` \| `local+upstream` |
| `acoustid.submit.upstream_app_key` | leer | Key für api.acoustid.org |
| `acoustid.update.time` | `04:00` | täglicher Delta-Import |
| `discogs.update.check_time` | `05:00` | täglicher Check auf neuen Monats-Dump |
| `discogs.token` | leer | Discogs-API-Token (Bilder; leer = Discogs-Bildquelle aus) |
| `tadb.api_key` | leer | TheAudioDB-Key (leer = Quelle aus) |
| `caa.crawl.enabled` | `false` | Voll-Spiegel-Crawler (Betreiber-Setup: an) |
| `caa.crawl.rate_per_s` | `2` | Crawler-Drossel |
| `covers.negative_retry_days` | `30` | Wiederholung bei "kein Cover gefunden" |
| `wake.hold_timeout_s` | `90` | Haltezeit beim Wecken |
| `idle.timeout_min` | `15` | Auto-Stopp der schlafenden Prozesse |
| `update.min_free_gb` | `100` | Mindest-Plattenreserve vor Import/Crawl |
| `cache.enabled` / `cache.max_size_mb` | `true` / `512` | Lookup-Cache |
| `metrics.enabled` | `false` | Prometheus |
| `notify.ntfy.url` / `notify.smtp.*` | leer | Kanäle (leer = aus) |
| `backup.dir` / `backup.time` / `backup.include_covers` | leer / `04:45` / `false` | Backup |
| `mb.dsn` | leer | Read-only-DSN MusicBrainz-Postgres |

Feste Werte:
- **Ports:** ein Port (Default `8080`) für alle APIs + `/admin`;
  per Env änderbar.
- **Cover-Ablage:** `/data/covers/<mbid[0:2]>/<mbid>.jpg`, genau eine
  Datei pro Release, max. 1200px lange Kante, JPEG.
- **Batch-Limit:** 100 Fingerprints pro `/v2/lookup/batch`-Request.
- **Upstream-Retry:** 7 Versuche, dann Notification + manueller Retry.
- **Idle-Definition:** keine externe Anfrage im Fenster UND kein
  laufender Job UND Crawler nicht aktiv.
- **Admin-Login:** ein Benutzer, argon2-Hash in SQLite, Erst-Passwort
  beim ersten Start generiert und geloggt.
- **Rate-Limits upstream (Beschaffung):** pro Quelle eigene interne
  Queue mit Drossel; Discogs gemäß deren Token-Limits, CAA gemäß
  `caa.crawl.rate_per_s` (gilt auch für Lazy-Abrufe), TheAudioDB gemäß
  Key-Klasse. Exakte Werte: Recherche §14.

## 8. Datenmodell

Exakte Spalten der Spiegel-Tabellen folgen den Quellformaten
(Recherche §14). Logisches Modell:

### PostgreSQL (Array) — Schema `acoustid`
Unverändert aus v1: `track`, `fingerprint`, `track_mbid`,
`local_submission` (Status `new`→`indexed`→`forwarded`|`forward_failed`),
`import_state` (Delta-Sequenz, resumierbar).

### PostgreSQL (Array) — Schema `discogs`
- `release`, `master`, `artist`, `label` + zugehörige
  Beziehungstabellen (Tracklists, Credits, Aliases, Bild-Verweise) —
  Struktur gemäß XML-Dump; etabliertes Schema-Tooling als Vorlage.
- `dump_state` — importierter Monats-Dump (Kennung, Zeitstempel,
  Zeilen), für Verfügbarkeits-Check und resumierbaren Import.

### PostgreSQL (Array) — Schema `covers`
- `artwork` — `release_mbid` (PK), Quelle (`caa`|`tadb`|`discogs`),
  Status (`ok`|`missing`|`failed`), Abmessungen, Dateigröße,
  `fetched_at`, `next_retry_at` (für Negativ-Einträge).
- `crawl_state` — Crawler-Cursor, Zähler (gefunden/fehlend/fehlerhaft),
  gestartet/aktualisiert.

### SQLite (Wächter, `/config`)
Unverändert aus v1: `api_key`, `admin_user`, `update_run`
(jetzt mit Job-Typ: acoustid-delta, discogs-dump, caa-crawl,
nachzügler, backup, queue-send), `event_log`, Lookup-Cache.

### Dateisystem
- `/data/covers/…` wie §7; `/data/tadb/…` JSON-Antworten + Bilddateien
  gespiegelt nach Anfragepfad.

### Externe Referenzen (nur lesend)
- MB-Postgres: Metadaten-Auflösung, `cover_art_archive`-Verzeichnis
  (Crawler-Quelle), MBID↔Discogs-Zuordnung über URL-Relationships.
  Gekapselte Query-Schicht (ein Modul, versionierbare Queries).

## 9. API-Spezifikation

Alle Pfade auf einem Port. Auth-Modus gilt einheitlich.

### AcoustID-kompatibel
`GET/POST /v2/lookup`, `POST /v2/submit`, `POST /v2/lookup/batch` —
unverändert aus v1 (Parameter, Antwortformat, Fehlerformat des
Originals; Batch max. 100).

### CAA-kompatibel
`GET /caa/release/{mbid}/front` (+ Größen-Suffixe des Originalschemas,
alle auf die lokale 1200px-Datei aufgelöst) — liefert das Bild direkt.

### Discogs-kompatibel (GET-Subset)
`GET /discogs/releases/{id}`, `/discogs/masters/{id}`,
`/discogs/artists/{id}`, `/discogs/labels/{id}` — Antwortstruktur der
Discogs-API nachgebildet, gespeist aus dem lokalen Spiegel.

### TheAudioDB-kompatibel
`GET /tadb/…` — transparentes Proxy-Pfadschema der Original-API;
Cache-Hit lokal, Miss → Upstream mit Betreiber-Key → cachen.

### Vereinheitlicht
- `POST /v1/identify` — `{fingerprint, duration, options}` →
  `{acoustid, recording/release (MB), discogs, tadb, cover_url, score}`.
- `GET /v1/release/{mbid}` — dasselbe Paket ohne Fingerprint.
- `GET /v1/cover/{mbid}` — Bild direkt; löst bei Miss die Kette aus.
Fehlende Teilquellen liefern `null`-Blöcke mit Grund
(`not_cached_offline`, `source_disabled`, `not_found`) — nie einen
Gesamtfehler.

### Betrieb
`GET /status` (weckt nie; Prozess-Zustände, Datenstände aller Quellen,
Crawl-Fortschritt, Version), `GET /metrics` (optional), `/admin/…`.

### Fehlerverhalten
- Wecken: Halten bis bereit, nach `wake.hold_timeout_s` → 503 +
  Retry-After.
- Cache-Miss ohne Internet (Lazy-Quellen) → 404 mit Klartextgrund
  `not cached, offline`.
- Quelle per Config aus (fehlender Key) → 404 mit `source_disabled`.
- Rate-Limit → 429 + Retry-After. Auth-Fehler im Format der jeweils
  nachgebildeten API.

## 10. Verhaltensregeln & Invarianten

1. **Nur der Supervisor startet/stoppt Prozesse.** API/Jobs steuern nie
   Prozesse direkt; kein docker.sock, kein Docker-Zugriff aus dem
   Container.
2. **Kein UI-/Status-Aufruf weckt das Array.** Admin-UI und `/status`
   arbeiten ausschließlich mit `/config`-Daten und dem letzten
   gemeldeten Job-Stand.
3. **Stale statt Sperre.** Importe (AcoustID-Deltas, Discogs-Dump)
   laufen transaktional; Lookups sehen bis zum Commit den alten Stand.
   Discogs-Monats-Import in Etappen (pro Entitätstyp/Datei eine
   Transaktion), resumierbar über `dump_state`.
4. **Alles resumierbar.** Delta-Import, Dump-Import, Crawler und
   Backups setzen nach Abbruch am letzten Cursor fort; fehlgeschlagene
   Läufe wiederholen sich beim nächsten Zyklus automatisch.
5. **Idle-Stopp nur im Ruhezustand** (keine Anfragen, keine Jobs,
   Crawler inaktiv). Ein aktiver Crawler hält das System wach — das ist
   während des Erst-Crawls beabsichtigt und in der UI sichtbar.
6. **Cache-Kohärenz.** Lookup-Cache-Invalidierung nach jedem
   erfolgreichen Import der betroffenen Quelle und nach lokalen
   Submissions; TADB-Cache nur manuell; Cover-Dateien sind
   quellenstabil (einmal geschrieben, nur per Admin-Aktion ersetzt).
7. **Ein Cover pro Release.** Kette CAA → TheAudioDB → Discogs; erste
   Quelle mit Treffer gewinnt; Ergebnis wird auf max. 1200px
   normalisiert; Fehlschlag aller Quellen → Negativ-Eintrag mit
   `next_retry_at`.
8. **Upstream-Höflichkeit.** Jede externe Quelle hat eine eigene
   Beschaffungs-Queue mit Drossel; Crawler und Lazy-Abrufe teilen sich
   die CAA-Queue. Bei 429/Sperren: exponentielles Backoff, Ereignis-Log.
9. **Degradierter Betrieb.** MB-Postgres nicht erreichbar → MBIDs ohne
   Metadaten; einzelne Quelle down/deaktiviert → `null`-Block in v1,
   404 mit Grund in den kompatiblen APIs. Nie Gesamtausfall wegen einer
   Teilquelle.
10. **Plattenplatz-Guard** vor jedem Import/Crawl-Segment:
    frei ≥ `update.min_free_gb`, sonst Stopp + Notification.
11. **Secrets nie im Repo**; `.env.example`/`config.example.yaml`
    dokumentieren alles.
12. **Ein Release = ein Image = ein Tag.**

## 11. Filestruktur (Repo)

```
musicmeta-offline/
├── Dockerfile                    # ein Image: Supervisor + Python-App
│                                 # + PostgreSQL + acoustid-index
├── docker-compose.yml            # ein Service, Volume-Zuordnung
├── .env.example
├── config.example.yaml
├── README.md                     # Unraid- + generisches Setup, Volumes
│                                 # Cache/Array, Bootstrap, Lizenzen
├── unraid/                       # Community-App-Template (XML)
├── supervisor/                   # Prozessdefinitionen, Start/Stopp-Hooks
├── app/
│   ├── watchdog/                 # Proxy, Scheduler, Prozess-Steuerung,
│   │                             # Admin-Routen, Auth, Cache, Notify, Metrics
│   │   ├── templates/            # Jinja2
│   │   └── static/
│   ├── api/                      # /v2, /caa, /discogs, /tadb, /v1
│   ├── importers/
│   │   ├── acoustid_delta/
│   │   └── discogs_dump/
│   ├── covers/                   # Kette, Crawler, Nachzügler, Normalisierung
│   ├── tadb/                     # Proxy-Cache
│   ├── mbref/                    # gekapselte MB-Query-Schicht
│   └── shared/                   # Config-Schema, Modelle, Queues, Logging
├── docs/
│   ├── HANDOFF.md                # dieses Dokument (v2)
│   └── DESIGN_HANDOFF.md         # v2
├── tests/
└── .github/workflows/            # ci.yml, release.yml (ein Image → GHCR)
```

## 12. CI/CD

- `ci.yml`: Lint + Tests bei jedem Push/PR.
- `release.yml`: bei Tag → ein Multi-Arch-Image (amd64, arm64) → GHCR
  `ghcr.io/<owner>/musicmeta-offline:<tag>` + `latest`.
- Eingebackene Postgres-/Index-Versionen sind im Image-Label und in
  `/status` sichtbar; Postgres-Major-Upgrade bekommt einen dokumentierten
  Migrationspfad (pg_upgrade-Schritt im Entrypoint, Recherche §14).

## 13. Deliverables der Implementierung

1. Lauffähiger Container gemäß §3–§11 inkl. Tests.
2. README (Unraid + generisch, Volume-Layout Cache/Array,
   Bootstrap-Anleitung mit realistischen Zeitangaben, Lizenzhinweise).
3. Unraid-Community-App-Template.
4. Actions-Workflows (§12).
5. Admin-UI gemäß DESIGN_HANDOFF.md v2 (Design entsteht in separater
   Claude-Design-Session).

## 14. Offene Punkte (Recherche zu Beginn)

1. **AcoustID:** JSON-Delta-Format im Detail; Existenz eines
   Voll-Snapshots für den Bootstrap (sonst Deltas seit Beginn);
   acoustid-index-Version, Fütterungs-/Abfrage-API, Rebuild-Kosten.
2. **Discogs:** exaktes XML-Schema der Monats-Dumps
   (Referenz: discogs-load / discogs-xml2db); Token-Rate-Limits der
   Bilder-API; Dump-Veröffentlichungsrhythmus/-URL für den
   Verfügbarkeits-Check.
3. **CAA:** offizieller Bulk-Bezugsweg (Internet-Archive-Bulk,
   rsync o. ä.), der den Erst-Crawl verkürzt; verträgliche Crawl-Rate;
   exaktes URL-/Größenschema.
4. **TheAudioDB:** API-Umfang je Key-Klasse, Rate-Limits,
   Cache-Erlaubnis laut ToS.
5. **MB-Spiegel:** benötigte Tabellen/Views inkl.
   `cover_art_archive`-Schema und URL-Relationships für
   MBID↔Discogs; Absicherung gegen Schema-Änderungen.
6. **Supervisor-Wahl:** s6-overlay vs. supervisord (Kriterium §5).
7. **Postgres-Upgrade-Pfad** im Ein-Container-Modell.
8. **Upstream-Submit** an api.acoustid.org: Format,
   Application-Key-Registrierung.

## 15. Risiken

### 15.1 Speicherbedarf (Schätzung, Verifikation im Bootstrap)
| Bestand | Schätzung |
|---|---|
| AcoustID-Postgres inkl. Indizes | 200–400 GB |
| acoustid-index | 30–80 GB |
| Discogs-Postgres inkl. Indizes | 100–180 GB |
| CAA-Voll-Spiegel (~2 Mio. Cover à 0,2–0,5 MB) | 500 GB–1,5 TB |
| TheAudioDB-Cache | einstellige GB (nutzungsgetrieben) |
| Import-/Staging-Puffer (temporär) | 50–100 GB |
| **Summe Array** | **~1–2 TB** |

### 15.2 Risikoliste
1. **Bootstrap-Aufwand (hoch):** AcoustID-Erst-Import (potenziell Tage)
   + CAA-Erst-Crawl (Wochen) + Discogs-Erst-Import auf Array-Spindeln.
   Zuerst Formate/Bulk-Wege verifizieren (§14), realistische Zeitplanung
   in die README.
2. **Ein-Container-Kopplung (mittel, bewusst akzeptiert):** Postgres/
   Index selbst gepflegt; jedes Update ersetzt alles; Supervisor muss
   Teilprozess-Abstürze sauber isolieren. Mitigation: Healthchecks pro
   Prozess, dokumentierter Upgrade-Pfad, konservative Basis-Versionen.
3. **Upstream-Sperren (mittel):** IA/CAA oder Discogs drosseln/sperren
   bei zu aggressivem Abruf. Mitigation: Queues, Backoff, konservative
   Defaults, Crawler abschaltbar.
4. **Postgres auf Spindeln (mittel):** langsame Importe; mitigiert durch
   Stale-Serving, Nachtfenster, etappenweisen Discogs-Import.
5. **MB-Schema-Kopplung (niedrig):** gekapselte Query-Schicht,
   degradierter Betrieb.
6. **Lizenz/ToS (niedrig):** keine Weiterverteilung; TheAudioDB-ToS zur
   Cache-Dauer prüfen (§14.4); README-Hinweise.

## 16. Migrationsanhang v1 → v2 (für das laufende Claude-Code-Projekt)

**Ausgangslage:** Projekt "acoustid-offline" ist auf v1-Basis in
Phase 19. Kein Neustart — Migration in Phasen.

### Weiter gültig (nicht anfassen, außer Umbenennung)
AcoustID-Fachlogik komplett: Lookup/Submit/Batch, Delta-Importer,
Index-Anbindung, Postgres-Schema `acoustid`, Lookup-Cache,
Config-System, Auth/API-Keys, Admin-UI-Gerüst, Notifications, Backup,
Rate-Limiting, Tests dieser Module.

### Gekippte v1-Entscheidungen (in DECISIONS.md dokumentieren)
| v1 | v2 | Grund |
|---|---|---|
| Stack aus 5 Containern, 2 Compose-Dateien | ein Container, eine Compose-Datei | Auftraggeber-Entscheidung |
| Getrennte Images Wächter/API/Importer | ein Image | folgt aus Ein-Container |
| Wächter steuert Docker via docker.sock | interner Prozess-Supervisor | docker.sock entfällt ersatzlos |
| Offizielle Postgres-/Index-Images | eingebacken im eigenen Image | folgt aus Ein-Container |
| Name acoustid-offline | musicmeta-offline | Scope-Erweiterung |
| `update.time` | `acoustid.update.time` (+ neue Scheduler-Keys) | mehrere Rhythmen |
| `update.min_free_gb` Default 50 | Default 100 | größere Bestände |
| CAA nicht im Scope | Voll-Spiegel + Lazy-Fallback | neue Anforderung |

### Migrationsphasen (Vorschlag; nach Impact-Analyse anpassen)
- **M0 — Impact-Analyse (Pflicht, zuerst):** ARCHITECTURE.md/
  PROGRESS.md gegen diesen Anhang prüfen: Wo steckt die
  Multi-Container-Annahme im Code (Docker-Steuerung, Netzwerknamen,
  Healthchecks, CI)? Ergebnis: konkrete Betroffenheitsliste + ggf.
  Anpassung der Phasenfolge. Erst danach weiterbauen.
- **M1 — Ein-Container-Umbau:** Supervisor einführen (§5-Entscheidung),
  Docker-Steuerung des Wächters durch Prozess-Steuerung ersetzen,
  Dockerfile/Compose/CI auf ein Image umstellen, Postgres + Index
  einbacken, Volume-Layout §3.
- **M2 — Umbenennung:** Repo, Image, Env-Prefix (`AOFF_` → `MMO_`),
  Config-Keys gemäß Tabelle oben, Doku.
- **M3 — Discogs-Spiegel:** Schema, Dump-Importer (etappenweise,
  resumierbar), Verfügbarkeits-Check, kompatibles GET-Subset.
- **M4 — Cover-Subsystem:** `covers`-Schema, Beschaffungskette,
  Normalisierung, CAA-Endpoint, Lazy-Fallback, Negativ-Cache.
- **M5 — CAA-Crawler:** Verzeichnis aus MB-Spiegel, Drossel, Resume,
  Nachzügler-Job, UI-Fortschritt.
- **M6 — TheAudioDB-Proxy-Cache.**
- **M7 — Vereinheitlichte v1-API** (`/v1/identify`, `/v1/release`,
  `/v1/cover`) inkl. MBID↔Discogs-Auflösung über `mbref`.
- **M8 — Admin-UI-Erweiterung** gemäß DESIGN_HANDOFF v2.
- **M9 — Abschluss:** README, Unraid-Template, Release-Workflow,
  Ende-zu-Ende-Tests, Bootstrap-Doku mit gemessenen Zeiten.

### Invarianten während der Migration
- Nach jeder Phase bleibt der AcoustID-Teil lauffähig (DroppedNeedle
  darf nie länger als eine Phase blockiert sein).
- Datenbestände aus v1 (AcoustID-Postgres, Index, Submissions) werden
  übernommen, nicht neu aufgebaut — Volume-Migration in M1
  dokumentieren.
- LEARNINGS.md sammelt Messwerte (Import-Dauern, Crawl-Raten,
  Bestandsgrößen) zur Korrektur von §15.1.
