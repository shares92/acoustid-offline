# ARCHITECTURE.md — musicmeta-offline

Statische technische Referenz. Quelle: [docs/HANDOFF.md](docs/HANDOFF.md) (Gesamtspezifikation,
**v2**; v1 archiviert unter docs/archive/HANDOFF-v1.md) und
[docs/DESIGN_HANDOFF.md](docs/DESIGN_HANDOFF.md) (Admin-UI, noch v1). Bei Widerspruch gilt
das Handoff. Änderungen an dieser Datei nur mit DECISIONS.md-Eintrag.

Stand: 2026-08-05 (v2-Stand nach M0/M1a/M1b/M2; DB-Spaltenschema aus Phase 0).

> **Vermerk 2026-08-05 (M2, Umbenennung):** Das Projekt hieß bis M2
> **acoustid-offline**; mit der Scope-Erweiterung auf vier Quellen
> (AcoustID, Discogs, Cover Art Archive, TheAudioDB) heißt es
> **musicmeta-offline** (HANDOFF v2 §16, DECISIONS 2026-08-04).
> §3/§4/§6/§10 beschreiben seit M2 den **gebauten v2-Stand** — ein
> Container, `supervisord` unter `tini`, Prozess- statt
> Container-Steuerung, eine Compose-Datei. Der frühere Vermerk „diese
> Datei beschreibt den v1-Stand" ist damit hinfällig.
>
> **Unverändert und testgekoppelt bleiben §5.1 (Ströme-Tabelle) und §5.2
> (DDL)** — v2 §8 bestätigt das Schema `acoustid`; Discogs/Covers kommen
> mit M3–M5 als **neue** Abschnitte daneben, nie als Edit an §5.2.
>
> **Vermerk 2026-08-05 (M2.5):** Scheduler, Notifications, Backup und
> Metrics sind gebaut. Damit weckt sich die Instanz erstmals **selbst**
> (§3, §8); die betroffenen Abschnitte tragen einen
> „Umsetzung (M2.5)"-Absatz.
>
> Noch **nicht gebaut** und deshalb hier als Plan zu lesen: alles zu
> Discogs, Covern, CAA und TheAudioDB (M3–M7) und die Admin-UI (M8).

---

## 1. Zielsetzung

Selbst gehostete, offline-fähige AcoustID-Instanz in **einem** Docker-Container:
Audio-Fingerprint-Lookup (Chromaprint → AcoustID-UUID → MusicBrainz-Recording
inkl. Metadaten) ohne Abhängigkeit vom öffentlichen api.acoustid.org.

**Erfolgskriterien:**
1. Standard-Clients (primär DroppedNeedle, außerdem Picard/beets per
   URL-Umbiegung) bekommen auf `/v2/lookup` korrekte, API-kompatible Antworten.
2. Im Normalzustand ruhen die Prozesse, die das Array brauchen — Postgres
   und API-Dienst sind gestoppt, die Array-Platten dürfen herunterfahren.
   Dauerhaft laufen nur der Wächter und der Suchindex, beide auf dem
   Cache-Pool (der Index bleibt resident, weil sein Kaltstart den ganzen
   Index liest — E12, §3).
3. Der Datenbestand aktualisiert sich täglich automatisch per Delta-Import,
   inkl. Selbst-Wecken und Wieder-Einschlafen.
4. Läuft auf jedem Docker-Host; Referenz-Deployment ist Unraid
   (DB auf dem Array; Suchindex und Wächter-Daten auf dem Cache-Pool).

## 2. Constraints

- **Host:** Unraid; Postgres auf dem Array (Spindeln), **Suchindex** und
  Wächter-Daten auf dem SSD-Cache-Pool (Index-Entscheid 2026-07-25: sein
  Kaltstart auf Spindeln endet im Timeout). Der Cache ist zu klein für den
  Datenbestand (dreistellige GB-Größe erwartet und akzeptiert), für den
  Index mit ~70 GB reicht er.
- **API-Kompatibilität** zu api.acoustid.org ist Pflicht (Drittclients).
- **MusicBrainz:** Lokaler Spiegel vorhanden (musicbrainz-docker-Stack,
  eigene Postgres). Direkter Read-only-DB-Zugriff wird genutzt.
- **Datenquelle:** Öffentliche AcoustID-Datenbank; tägliche inkrementelle
  JSON-Update-Dateien (data.acoustid.org). Lizenz CC BY-SA 3.0;
  MusicBrainz-AcoustID-Mapping Public Domain.
- **Netz:** Primär LAN/VPN. Bei Exponierung nach außen zwingend
  `apikey`-Modus + Reverse-Proxy mit TLS (Doku-Hinweis).
- **Repo:** Öffentlich auf GitHub; GitHub Actions bauen **ein** Image nach
  GHCR; ein Release = ein Image = ein Tag.
- **Kein Fingerprint-Berechnen serverseitig** (Chromaprint läuft im Client).

## 3. Architektur-Überblick

Ein Repo, **ein Image, ein Container, eine Compose-Datei**. Was in v1 fünf
Container waren, sind seit M1b Prozesse in diesem einen Container; gesteuert
werden sie von `supervisord` unter `tini` als PID 1
(`supervisor/supervisord.conf`). **Kein docker.sock mehr** — der Wächter
spricht mit `supervisord` über dessen Unix-Socket (`/run/supervisor.sock`,
0700, kein `inet_http_server`).

**Prozesse im Container:**

| Prozess | Aufgabe | Lebenszyklus | Benutzer |
|---|---|---|---|
| `watchdog` | Reverse-Proxy mit Weck-Logik, Scheduler, Admin-UI, Lookup-Cache, Notifications, Metrics | läuft immer (`autorestart=true`) | root — er steuert supervisord (§8.1) |
| `index` | acoustid-index (`fpindex`) — Fingerprint-Suchindex, Matching-Kern | **resident** (E12): sein Kaltstart liest den gesamten Index, und auf dem Cache-Pool hält er kein Array wach | `acoustid` (6081) |
| `db` | PostgreSQL: AcoustID-Datenbestand + lokale Submissions | schläft; der Wächter startet und stoppt ihn | `postgres` (999) |
| `api` | `/v2/lookup`, `/v2/submit`, Batch-Endpoint, MB-Metadaten-Auflösung | schläft; der Wächter startet und stoppt ihn | `api` — der einzige Prozess mit Fremdeingaben läuft **unprivilegiert** |

**Jobs stehen bewusst nicht in der supervisord-Konfiguration** (E10):
Importer, Crawler und Backup brauchen Per-Lauf-Argumente, die
`[program:*]` nicht übergeben kann — sie laufen seit M2.5 als direkte
Subprozesse des Wächters (Argumente, Returncode und Report ohne Umweg).

**Volumes** — die Aufteilung entscheidet, ob das Array je schläft:

| Mount | Ablage | Inhalt |
|---|---|---|
| `/config` | **Cache** | `config.yaml`, Wächter-SQLite, Lookup-Cache, Logs, DB-Passwort |
| `/index` | **Cache** | acoustid-index (~70 GB einplanen; Kaltstart auf Spindeln = Timeout) |
| `/data/db` | Array | PostgreSQL (`/data/db/<major>`, E14) |
| `/import` | Array | Dump-Downloads und Staging |
| `/backup` | Array | Sicherungen (`backup.dir`; Restore: docs/backup-restore.md) |

In v2 ist `/data` das **Array** — Wächter-Daten gehören nach `/config`
(Risiko R1 der M0-Analyse). Das Docker-Image selbst gehört ebenfalls auf
den Cache, sonst hält der laufende Container das Array wach. Im Release
sind es **Bind-Mounts**, keine benannten Volumes (E13): `docker compose
down -v` nähme sonst später bis zu 2 TB Bestand mit.

**Ports:** genau **ein** veröffentlichter Port (Default `8080`) für
API-Proxy und Admin-UI unter `/admin`. Postgres (5432), Suchindex (6081)
und API-Dienst (8081) lauschen nur auf dem containerinternen Loopback; der
Suchindex hat keine Auth und darf nie nach außen.

**Schlaf-Logik (prozessintern):** „schlafend" heißt, dass `db` und `api`
gestoppt sind; auf `/data/db` passiert kein I/O, die Array-Platten dürfen
herunterfahren. Der Wächter beantwortet `/status` und die Admin-UI
ausschließlich aus `/config`. Eine eingehende API-Anfrage lässt ihn die
Prozesse der Reihe nach starten — sequenzieller Start mit Readiness-Gates,
die Datenbank hart über `pg_isready`; die Anfrage wird währenddessen
gehalten (§7). **Seit M2.5 weckt auch ein fälliger Job** — das ist der
einzige Weg, auf dem die Instanz von selbst aufwacht.

**Datenflüsse:**
- Client → Wächter (Rate-Limit → Auth → Lookup-Cache) → API-Prozess →
  Suchindex (Match) + eigene Postgres (Mappings) + MB-Postgres
  (Metadaten, read-only).
- Scheduler (Wächter) → weckt die Prozesse → startet den Importer-Job als
  Subprozess → Deltas einspielen → Lookup-Cache invalidieren → wieder
  schlafen legen.

**Umsetzung (M2.5).** Der Zeitplan liegt in
`watchdog/app/scheduler.py` (zwei Termine aus §6: `acoustid.update.time`
und — nur mit eingerichtetem `backup.dir` — `backup.time`), der Ablauf
eines Laufs in `watchdog/app/jobs.py`:

1. Lauf in `update_run` anlegen (blockiert den Idle-Stopp, §8.5),
2. Plattenplatz je Schreibpfad prüfen (§8.8, E11),
3. wecken — mit eigener, großzügiger Frist: dort wartet ein Job, keine
   Anfrage,
4. Job als Subprozess starten, Returncode und `--report` auswerten,
5. nach einem erfolgreichen Delta-Import den Lookup-Cache invalidieren
   (§8.6) und die Upstream-Warteschlange abarbeiten (§8.9) — als
   **eigener** Lauf, damit ein gescheiterter Versand keinen erfolgreichen
   Datenabgleich rot färbt,
6. schlafen legen, **wenn** der Zyklus den Stack selbst geweckt hat und
   während des Laufs keine `/v2/`-Anfrage kam (sonst übernimmt der
   Idle-Stopp).

Fällig heißt „seit dem heutigen Termin lief noch keiner": ein
30-Sekunden-Takt trifft eine Minute nie sicher, und die Historie überlebt
einen Neustart. Verpasste Termine werden am selben Tag nachgeholt.
Genau **ein** Job läuft gleichzeitig; derselbe Weg trägt die interne
Trigger-API für manuelle Läufe (Grundlage von `/admin/jobs`, M8).
- Admin-UI läuft vollständig im Wächter; Aktionen, die die Datenbank
  brauchen, zeigen den Schlafzustand und bieten einen Weck-Button.

**Ab M3 kommen dazu** (v2 §3/§4): Discogs-Dump-Spiegel, Cover-Ablage
(`/data/covers`), TheAudioDB-Cache (`/data/tadb`) und der CAA-Crawler.

**Grundsatzentscheidungen:** siehe DECISIONS.md — 2026-07-25 für den
AcoustID-Kern, 2026-08-04 für die Übernahme von HANDOFF v2 und die
Ein-Container-Entscheide E1–E16.

## 4. Technologie-Stack

Immer neueste stabile Version zum Implementierungszeitpunkt:

- **Sprache:** Python 3.14 (API, Importer, Wächter — eine Sprache für alles)
- **Web-Framework:** FastAPI (API + Admin-UI-Routen)
- **UI:** Server-rendered — Jinja2-Templates + HTMX, kein Frontend-Build,
  kein SPA-Framework, kein npm
- **Prozess-Supervisor:** `supervisord` unter `tini` als PID 1 (gewählt
  gegen s6-overlay, DECISIONS 2026-08-04 E1; Kriterium war das saubere
  Start/Stopp einzelner Dienste zur Laufzeit über einen Socket).
  Supervision-Politik E15: `autorestart=unexpected` für `db`, `index` und
  `api` — per Stopp-Befehl Gestopptes bleibt gestoppt (kein
  Idle-Stopp-Loop), Abstürze werden geheilt; `autostart=false` für die
  Schlafgruppe `db`/`api`, während `index` mit dem Container startet
  (resident, E12) und der Wächter mit `autorestart=true` läuft
- **Datenbanken:** PostgreSQL (eingebacken, genau **eine** Major je Image
  mit Versions-Drift-Guard, E14), SQLite (Wächter-Zustand + Lookup-Cache)
- **Suchindex:** acoustid-index (`fpindex`), aus der Quelle gebaut und mit
  Commit-Pin eingebacken — GPL-3.0-or-later, deshalb
  THIRD-PARTY-NOTICES.md + Quelltext im Image (E7)
- **Paketierung:** uv-Workspace, ruff + pytest
- **Deployment:** ein Docker-Image, **eine** Compose-Datei (ein Service),
  Bind-Mounts statt benannter Volumes (E13); Image via GHCR
- **CI:** GitHub Actions — `ci.yml` (Lint, Unit-, Integrations- und
  Image-Tests, Bit-Verifikation der Extension), `release.yml` (ein Tag →
  **ein** Image → GHCR; vorerst `linux/amd64` only, E3)

## 5. Datenmodell

Verifiziert in Phase 0 (2026-07-25): Feldstruktur dreifach belegt —
Exporter-Quellcode (`github.com/acoustid/acoustid`, `pkg/export`),
Live-Daten von data.acoustid.org und lokale Fixtures
(`tests/fixtures/acoustid-dumps/`, Tag 2026-07-22 komplett + Edge Cases).

### 5.1 Datenquelle: tägliche JSONL-Deltas

- Pfadschema `https://data.acoustid.org/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>-<strom>-update.jsonl.gz`;
  gzip-JSONL, ein Objekt pro Zeile. Historie lückenlos seit **2011-08-19**
  (Stand 25.07.2026: 5.454 Tage, 38.178 Dateien, **414 GB gz**, davon
  ~94 % `fingerprint`); laufend ~58 MB/Tag. Frei zugänglich, Range-Requests
  möglich. Discovery über `index.json` je Verzeichnis (Array aus
  `{"name","size"}`; Verzeichniseinträge kommen ohne `size`; Listings
  existieren auch auf Jahres-/Wurzelebene; exakte Bytegrößen — die
  HTML-Listings labeln Binärpräfixe falsch als SI).
- **Kein Voll-Snapshot** (`ExportTableFull` unimplementiert; Alt-Dumps 404).
  Bootstrap = vollständiger Replay aller Tagesdateien, zur Laufzeit als
  resumierbarer Importer-Job — Daten stecken nie in den Images (DECISIONS).
- Export ist `COPY (… row_to_json … json_strip_nulls …) TO STDOUT`:
  **fehlender JSON-Schlüssel bedeutet NULL bzw. false**, nie „unverändert".
- **COPY-Text-Escaping ist epochenabhängig (Korrektur Phase 6):**
  Dateien **bis einschließlich 2024-12-04** tragen zusätzlich das
  COPY-Escaping (Backslashes verdoppelt) — praktisch relevant nur im
  Freitext-Strom `meta` (0,5–2,5 % der Zeilen je Tag sind roh kein
  gültiges JSON; Tag 1 der Historie bricht sonst in Zeile 128). **Ab
  2024-12-05** sauberes JSONL. Achtung: Alt-Zeilen können auch
  *zufällig* parsen und dann falsche Werte liefern — die Lesart muss
  nach Datei-Epoche gewählt werden (`COPY_TEXT_LAST_DAY = 2024-12-04`,
  zeilenweiser Fallback mit Zähler). Beleg: Stichproben 2011–2026 +
  Fixture `2011-08-19-meta-update` (85/85 Zeilen per Unescape
  wiederhergestellt, unabhängig doppelt verifiziert).
- **Upsert-Strom ohne Operations-Marker:** jede Zeile ist ein Upsert auf
  PK `id`. DELETEs werden nie übertragen — Track-Merges kommen als
  `track.new_id` (ggf. verkettet), deaktivierte MBID-Zuordnungen als
  `track_mbid.disabled: true` (Schlüssel erscheint NUR bei true; bei
  Fehlen explizit auf false setzen — Reaktivierungs-Falle). Verwaiste
  Detailzeilen gemergter Tracks bleiben stehen → kein UNIQUE auf
  `(track_id, mbid)` möglich.
- `fingerprint-update` exportiert nur nach `created` (Vektor faktisch
  immutable); `track_fingerprint-update` ist eine zweite Projektion
  **derselben** Upstream-Tabelle (`id == fingerprint_id`) und liefert
  `track_id`/`submission_count`/`updated`.
- Keine Checksummen, Sequenznummern oder Manifeste: **Lückenprüfung
  (Kalendertage je Strom) ist Importer-Pflicht.** Leere Dateien
  (23-Byte-gz) sind legitim (`track_puid` meist — aber nicht immer:
  2011 Massendaten, Juli 2026 wieder 9 gefüllte Tage; Daten-Flaute
  seit 2026-07-22). Neuester verfügbarer Tag ist der Vortag.
- **Akzeptierte Lücke:** Zeilen von vor 2011-08-19, die seither nie
  geändert wurden, fehlen prinzipbedingt (Obergrenze ~10 % des Bestands).
- **Fingerprint-Kodierung:** JSON-Array vorzeichenbehafteter int32
  (dekodierte Chromaprint-Hashes; Algorithmusversion nicht enthalten;
  ≠ Base64-Wire-Format der Submit-API). In den acoustid-index gehen nur
  **Query-Extrakte** (Offset 80, max. 120 Hashes, 28-Bit-Maske
  `& 0xFFFFFFF0`, Silence-Hash `627964279` gefiltert, unsigned) —
  Python-Nachbau von `acoustid_extract_query`; pg_acoustid wird nicht
  eingesetzt (DECISIONS).

| Strom | Zieltabelle | Felder (optionale in Klammern) | Anteil gz |
|---|---|---|---|
| `fingerprint-update` | `fingerprint` | `id`, `fingerprint` int32[], `length`, `created` | 390 GB |
| `track_fingerprint-update` | `fingerprint` | `id`, `track_id`, `fingerprint_id`(==id), `submission_count`, `created`, (`updated`) | 1,6 GB |
| `track-update` | `track` | `id`, `gid` uuid, (`new_id`), `created`, (`updated`) | 2,3 GB |
| `track_mbid-update` | `track_mbid` | `id`, `track_id`, `mbid` uuid, `submission_count`, (`disabled`), `created`, (`updated`) | 1,1 GB |
| `meta-update` | `meta` | `id`, (`track`, `artist`, `album`, `album_artist`, `track_no`, `disc_no`, `year`, `created`) | 11,6 GB |
| `track_meta-update` | `track_meta` | `id`, `track_id`, `meta_id`, `submission_count`, `created`, (`updated`) | 7,2 GB |
| `track_puid-update` | `track_puid` | `id`, `track_id`, `puid` uuid, `submission_count`, `created`, (`updated`) — meist leer, wird regulär geparst | 0,3 GB |

Alle 7 Ströme werden geladen und importiert (Betreiber-Entscheid
2026-07-25). Upstream existierende, aber **nicht exportierte** Spalten:
`meta.gid`, `track_mbid.merged_into`, Fingerprint-Version.
`meta`-Zeilen mit `created IS NULL` erscheinen nie im Delta → FK
`track_meta.meta_id → meta.id` ist nicht erzwingbar.

### 5.2 PostgreSQL (Stack, Array) — Schema

Bookkeeping-Spalten `src_day`/`imported_at` sind projektspezifisch [P],
alles andere direkt aus dem Dump [D]. FKs und der endgültige
Sekundärindex-Satz werden in Phase 4 festgelegt und beim Bootstrap erst
**nach** dem Massenimport angelegt.

```sql
CREATE TABLE track (
    id          integer      PRIMARY KEY,
    gid         uuid         NOT NULL,              -- öffentliche AcoustID
    new_id      integer      NULL,                  -- Merge-Ziel (ggf. verkettet)
    created     timestamptz  NOT NULL,
    updated     timestamptz  NULL,
    src_day     date         NOT NULL,              -- [P] Tagesdatei der letzten Anwendung
    imported_at timestamptz  NOT NULL DEFAULT now() -- [P]
);
CREATE UNIQUE INDEX track_idx_gid    ON track (gid);
CREATE INDEX  track_idx_new_id ON track (new_id) WHERE new_id IS NOT NULL;

-- Zwei Ströme befüllen disjunkte Spaltenmengen derselben Entität,
-- daher fachlich NOT-NULL-Spalten hier bewusst nullable:
CREATE TABLE fingerprint (
    id               integer     PRIMARY KEY,        -- == fingerprint_id
    fingerprint      integer[]   NULL,               -- voller signed-int32-Vektor
    length           integer     NULL,               -- Sekunden (upstream small-/integer uneinheitlich)
    track_id         integer     NULL,
    submission_count integer     NULL,
    created          timestamptz NULL,
    updated          timestamptz NULL,
    indexed_at       timestamptz NULL,               -- [P] an acoustid-index übergeben
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);
CREATE INDEX fingerprint_idx_track_id   ON fingerprint (track_id);
CREATE INDEX fingerprint_idx_incomplete ON fingerprint (id)
    WHERE fingerprint IS NULL OR track_id IS NULL;   -- [P] nach Voll-Replay leer!
CREATE INDEX fingerprint_idx_unindexed  ON fingerprint (id)
    WHERE indexed_at IS NULL;                        -- [P] Arbeitsvorrat Index-Feed
ALTER TABLE fingerprint ALTER COLUMN fingerprint SET COMPRESSION lz4;

CREATE TABLE track_mbid (
    id               integer     PRIMARY KEY,
    track_id         integer     NOT NULL,
    mbid             uuid        NOT NULL,           -- MusicBrainz-Recording
    submission_count integer     NOT NULL,
    disabled         boolean     NOT NULL DEFAULT false, -- Schlüssel fehlt => false setzen
    created          timestamptz NOT NULL,
    updated          timestamptz NULL,
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);
CREATE INDEX track_mbid_idx_track_id ON track_mbid (track_id, mbid); -- bewusst NON-UNIQUE (Merge-Waisen)
CREATE INDEX track_mbid_idx_mbid     ON track_mbid (mbid);

CREATE TABLE meta (
    id           integer     PRIMARY KEY,
    track        varchar     NULL,
    artist       varchar     NULL,
    album        varchar     NULL,
    album_artist varchar     NULL,
    track_no     integer     NULL,
    disc_no      integer     NULL,
    year         integer     NULL,
    created      timestamptz NULL,                   -- upstream nullable (Delta-Lücke, s. o.)
    src_day      date        NOT NULL,               -- [P]
    imported_at  timestamptz NOT NULL DEFAULT now()  -- [P]
);

CREATE TABLE track_meta (
    id               integer     PRIMARY KEY,
    track_id         integer     NOT NULL,
    meta_id          integer     NOT NULL,           -- FK auf meta NICHT erzwingbar
    submission_count integer     NOT NULL,
    created          timestamptz NOT NULL,
    updated          timestamptz NULL,
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);
CREATE INDEX track_meta_idx_track_id ON track_meta (track_id);

CREATE TABLE track_puid (                            -- Legacy; bleibt praktisch leer,
    id               integer     PRIMARY KEY,        -- Strom läuft in der Lückenprüfung mit
    track_id         integer     NOT NULL,
    puid             uuid        NOT NULL,
    submission_count integer     NOT NULL,
    created          timestamptz NOT NULL,
    updated          timestamptz NULL,
    src_day          date        NOT NULL,           -- [P]
    imported_at      timestamptz NOT NULL DEFAULT now() -- [P]
);

CREATE TABLE import_state (                          -- [P] Buchführung resumierbarer Import
    stream      text        NOT NULL,                -- 'track' | 'fingerprint' | ...
    day         date        NOT NULL,
    file_name   text        NOT NULL,
    file_size   bigint      NULL,
    row_count   bigint      NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz NULL,
    PRIMARY KEY (stream, day)
);

CREATE SEQUENCE local_submission_track_id_seq AS integer MINVALUE 1 MAXVALUE 2147483647 NO CYCLE;

CREATE TABLE local_submission (
    id               bigserial   PRIMARY KEY,           -- [P] Submission-ID der Antwort
    local_track_id   integer     NOT NULL,              -- [P] Gruppe + Dokument-ID (+2^31)
    local_track_gid  uuid        NOT NULL,              -- [P] ausgelieferte AcoustID
    status           text        NOT NULL DEFAULT 'new',-- [P] new|indexed|forwarded|forward_failed
    fingerprint      integer[]   NOT NULL,              -- voller signed-int32-Vektor (Rescoring)
    length           integer     NOT NULL,              -- `duration.N` in Sekunden, 1…32767
    bitrate          integer     NULL,                  -- `bitrate.N`
    fileformat       varchar     NULL,                  -- `fileformat.N`
    mbid             uuid        NULL,                  -- `mbid.N` (eine Zeile je MBID)
    puid             uuid        NULL,                  -- `puid.N` (Legacy)
    foreignid        varchar     NULL,                  -- `foreignid.N`, Form vendor:id
    track            varchar     NULL,                  -- `track.N`
    artist           varchar     NULL,                  -- `artist.N`
    album            varchar     NULL,                  -- `album.N`
    album_artist     varchar     NULL,                  -- `albumartist.N`
    track_no         integer     NULL,                  -- `trackno.N`
    disc_no          integer     NULL,                  -- `discno.N`
    year             integer     NULL,                  -- `year.N`
    client           varchar     NULL,                  -- `client` (Application-Key)
    client_version   varchar     NULL,                  -- `clientversion`
    submitted_by     varchar     NULL,                  -- `user` (Phase 12 reicht ihn durch)
    created          timestamptz NOT NULL DEFAULT now(),
    indexed_at       timestamptz NULL,                  -- [P] an acoustid-index uebergeben
    forwarded_at     timestamptz NULL,                  -- [P] Phase 12
    forward_attempts integer     NOT NULL DEFAULT 0,    -- [P] Phase 12 (Grenze 7, §8.9)
    forward_error    text        NULL,                  -- [P] Phase 12
    CONSTRAINT local_submission_status_check
        CHECK (status IN ('new', 'indexed', 'forwarded', 'forward_failed'))
);
ALTER TABLE local_submission ALTER COLUMN fingerprint SET COMPRESSION lz4;
CREATE INDEX local_submission_idx_unindexed ON local_submission (id)
    WHERE status = 'new';                                -- [P] Arbeitsvorrat Indexierung
CREATE INDEX local_submission_idx_track_id  ON local_submission (local_track_id);
CREATE INDEX local_submission_idx_track_gid ON local_submission (local_track_gid);
```

**Import-Regeln (Invarianten des Importers):**
1. Reihenfolge je Tag: `track` → `meta` → `fingerprint` +
   `track_fingerprint` → `track_mbid` → `track_meta` → `track_puid`;
   Tage strikt chronologisch ab 2011-08-19.
2. Upsert per `INSERT … ON CONFLICT (id) DO UPDATE`; für `fingerprint`
   zwei getrennte Upserts mit disjunkten `DO UPDATE SET`-Spaltenmengen
   (`created` per COALESCE, der jeweils andere Strom überschreibt seine
   Spalten nicht).
3. Absent⇒Default-Regel: fehlender Schlüssel = NULL, `disabled` = false
   (explizit setzen, nie „unverändert lassen").
4. Direkter zeilenweiser JSON-Parse (kein COPY-FROM-Staging — würde
   valides JSONL korrumpieren); Parse-Fehler = harter Abbruch der
   Datei-Transaktion.
5. Lückenprüfung über Kalendertage je Strom (`generate_series` gegen
   `import_state`); leere Datei ok, fehlende Datei = Fehler.
6. Bootstrap-Bulk-Modus: Sekundärindizes/FKs erst nach dem Massenimport,
   unsichere PG-Bulk-Einstellungen nur währenddessen und danach
   zurücknehmen (Details Phase 8).

**Umsetzung (Phase 4):** Migrations-Runner in `shared/shared/db/`
(CLI `python -m shared.db`), SQL in den Gruppen `core` (Tabellen+PKs,
inkl. lz4-Compression — muss vor dem Massenimport gesetzt sein) und
`indexes` (Sekundärindizes, beim Bootstrap nachgezogen); idempotent,
Checksummen-Drift-Erkennung, Advisory-Lock. Compose-Service `db` =
`postgres:18` (Achtung PG-18-Volume-Layout: Mount auf
`/var/lib/postgresql`). Ein Test hält das DDL in diesem Abschnitt und
die Migrations-SQL anweisungsgleich.

**Umsetzung (Phase 7):** `importer/app/upserts.py` (je Strom ein
Statement + Record-Übersetzung, pure; Disjunktheits-Selbsttest beim
Modul-Import), `state.py` (`import_state`-Buchführung, bindet die
Phase-6-Arbeitsliste an) und `dbimport.py` (`import_file`: eine
Tagesdatei = genau eine Transaktion; lehnt Verbindungen mit offener
Transaktion ab). Semantik-Details: `created` bei `fingerprint` per
`COALESCE(bestehend, neu)`; `src_day`/`imported_at` schreiben beide
Fingerprint-Ströme (Buchführung gehört keinem Strom);
`track_fingerprint.fingerprint_id != id` ist ein harter Fehler;
`import_state.finished_at` per `clock_timestamp()` (Dauer messbar).
Begründungen: DECISIONS „Phase-7-Import-Details".

**Umsetzung (Phase 8):** `importer/app/job.py` (Gesamtlauf: Guard →
`core` → Massenimport im Bulk-Modus mit Prefetch → `CHECKPOINT` →
`indexes` → Index-Feed), `bulk.py` (Regel 6: ausschließlich
`synchronous_commit=off`, sitzungsweit, Rücknahme auf den Vorher-Wert;
`fsync`/`full_page_writes`/`ALTER SYSTEM` bewusst nie), `diskguard.py`
(§8.8; misst das Dump-Verzeichnis — das PG-Datenverzeichnis ist aus dem
Container nicht sichtbar), `prefetch.py`, `measure.py`, `report.py`
(Exit-Codes + Report-Schema) und `__main__.py` (CLI). Details, Aufrufe
und Report-Format: [docs/importer-job.md](docs/importer-job.md).

**Umsetzung (Phase 11):** `local_submission` (DDL oben im Block;
Migrationen core/0008 + indexes/0105) hält eigene Einreichungen —
bewusst **nie** in den sieben Dump-Tabellen (deren Upsert schreibt
ganze Zeilen per expliziter ID und würde lokale Einträge still
überschreiben). Eine Zeile je eingereichter MBID; `local_track_id`
gruppiert die Zeilen einer Aufnahme und ist zugleich — versetzt um
2^31 — die Dokument-ID im Suchindex (reservierter Bereich
[2^31, 2^32-1], typbedingt disjunkt zu `fingerprint.id`; §5.3).
Status `new` → `indexed` (Phase 11) → `forwarded` | `forward_failed`
(Phase 12). Details: [docs/api-submit.md](docs/api-submit.md),
DECISIONS „Phase-11-Submit-Details".

### 5.3 Matching-Pipeline (verifiziert in Phase 1)

Details: [docs/research/phase1-acoustid-index.md](docs/research/phase1-acoustid-index.md).

- **Zweistufig:** (1) Kandidaten aus dem acoustid-index
  (`POST /:index/_search`, limit 20–40, timeout 2000 ms, msgpack;
  Score dort = Integer-Trefferzahl, dient nur der Vorsortierung;
  Dokument-IDs sind **u32** — empirisch verifiziert, Phase 11 —,
  `[0, 2^31-1]` gehört dem Delta-Bestand (`fingerprint.id`),
  `[2^31, 2^32-1]` den lokalen Submissions);
  (2) **Rescoring in Python** (Nachbau `acoustid_compare2`,
  max_offset 80) gegen den Vollvektor aus Postgres; Längenfilter
  `length ± 7`; Ergebnis-Score float, Cutoff > 0,4 (Lookup) bzw.
  > 0,75 (Merge-Entscheidungen), max. 10 Ergebnisse, dedupliziert.
- **CI-Bit-Verifikation:** Python-`extract_query` und -`compare2`
  werden in CI bit-genau gegen die Original-C-Extension geprüft
  (pg_acoustid nur als Test-Container; die offizielle Python-Referenz
  von `extract_query` ist nachweislich defekt — nicht kopieren).
  Umgesetzt in Phase 9: `tests/pg_acoustid/` (PG 18 + Extension,
  Commit-gepinnt, Quelltext zur Bauzeit geholt), eigener CI-Job,
  pytest-Marker `extension` (`ACOUSTID_EXTENSION_DSN`). Die
  Verifikation deckte einen Fehler im Phase-5-`extract_query` auf
  (Startoffset in bereinigter Kopie statt Rohvektor) — behoben, bevor
  je ein Fingerprint indexiert wurde.
- **Umsetzung Matching (Phase 9):** `compare2`-Nachbau in
  `shared/shared/fingerprint/compare.py` (Vorlage
  `match_fingerprints2` aus `acoustid_compare.c`, inkl. dreier
  Bug-für-Bug-Eigenheiten — 14-Bit-Präfix in der Vielfaltszählung,
  Ausrichtungsschleife bis MATCH_MASK exklusiv, teilgelöschter
  `seen`-Puffer); Chromaprint-Codec in
  `shared/shared/fingerprint/chromaprint.py`; Pipeline in
  `api/app/matching.py` (limit 40, timeout 2000 ms, Cutoff >0,4,
  Kappung auf 10 vor der Track-Deduplizierung, Merge-Verkettung über
  `track.new_id`). Gemessen: ~0,39 ms Rescoring je Kandidat.
- **Query-Extraktion (präzisiert Phase 9):** `clean_size` = Anzahl
  Nicht-Silence-Hashes bestimmt den Startoffset
  `max(0, min(clean_size − max_hashes, 80))`; der Offset zeigt in den
  **Rohvektor** (Stille wird gezählt, nicht entfernt). Ab dort:
  Silence-Hash 627964279 überspringen, 28-Bit-Maske `& 0xFFFFFFF0`,
  dedupliziert, max. `acoustid.index.query_hashes` Hashes (Default 120),
  unsigned. Änderung der Hash-Anzahl erfordert Index-Neuaufbau.
- **Index-Feed:** aufsteigend nach `fingerprint.id` (~15 % kleinerer
  Index), Batches à 1000 via `_update` (atomar; `expected_version`
  für Idempotenz), msgpack.
- **Betrieb:** Indexdaten 40–55 GB erwartet (Volume ~70 GB) auf dem
  SSD-Cache-Pool, Unraid-Share „Prefer/Only: Cache"; der Start lädt
  alle Daten (Healthcheck-`start_period` ~15 min); Container-UID 6081;
  Timeout-Antworten sind HTTP 500; `GET /_metrics` (Prometheus);
  Backup via `GET /:index/_snapshot` (tar, ohne Oplog) oder
  Dateikopie im Stillstand.
- **Umsetzung (Phase 5):** Client in `shared/shared/fpindex/`
  (query/wire/client/errors); Indexname via Bootstrap-Variable
  `MMO_INDEX_NAME` (Default `main`). Compose-Healthcheck prüft
  `/<name>/_health` und wird erst nach `ensure_index()` gesund —
  importer hängt mit `service_started` ab, api mit `service_healthy`.
  Empirische API-Befunde: Addendum in
  docs/research/phase1-acoustid-index.md.
- **Umsetzung Index-Feed (Phase 7):** `importer/app/indexfeed.py`
  (`feed_index`): Arbeitsvorrat = Partialindex
  `fingerprint_idx_unindexed`, Paging per `id > zuletzt gesehen`;
  Reihenfolge **erst** Index-`_update`, **dann** `indexed_at` (umgekehrt
  wäre stiller Datenverlust; doppeltes Senden ist idempotent). Zeilen
  ohne Vektor bleiben im Vorrat; Vektoren ohne indexierbare Hashes
  gelten als erledigt (gezählt + geloggt). Jeder Batch mit
  `expected_version` abgesichert (zweiter Schreiber ⇒ lauter Abbruch).
  Im Bootstrap läuft der Feed erst **nach** der Migrationsgruppe
  `indexes` — ohne `fingerprint_idx_unindexed` wäre jeder Batch ein
  Seq-Scan (Phase 8).

### 5.4 MusicBrainz-Query-Schicht (verifiziert in Phase 1)

Details: [docs/research/phase1-mb-schema.md](docs/research/phase1-mb-schema.md).

- **Eine Datei kennt MB** (`mb/queries.py`, Raw-SQL, kein
  mbdata-Paket): 10 Batch-Funktionen (health, selfcheck, redirects,
  Existenzprüfung, recordings, artist_credits, release_rows mit
  Zeilen-Kappung, release_counts, release_events, release_groups +
  secondary_types). Alles schema-qualifiziert, explizite
  Spaltenlisten, `= ANY(:arr)`. CI-Grep-Regel: `musicbrainz\.` nur in
  dieser Datei.
- **17 MB-Tabellen** inkl. `recording_gid_redirect` und
  `replication_control`; Schema-Sequenz aktuell 31 (ändert ~1×/Jahr,
  bisher nur additiv → Risiko niedrig); Selfcheck beim Start.
- **Redirect-Auflösung:** nicht gefundene Recording-MBIDs werden
  online gegen `recording_gid_redirect` aufgelöst (Antwort trägt die
  kanonische MBID; Config-Flag `mb.keep_submitted_mbid` zum
  Durchreichen der eingereichten). Umgesetzt seit Phase 10 in
  `shared/shared/mb/` (Choreografie in `metadata.py`, Antwortaufbau in
  `api/app/meta.py`).
- **Degradierter Betrieb:** `MbUnavailable` (Connect/Timeout/Pool) ⇒
  Antwort ohne Metadaten, HTTP 200; Circuit-Breaker;
  `statement_timeout` ~2 s; `connect_timeout` 2 s; eigener kleiner
  Pool. Staleness (Spiegel repliziert täglich): WARN > 36 h,
  CRIT > 7 d.
- **Read-only-Rolle** `acoustid_ro` per dokumentiertem SQL-Snippet
  (Betreiber führt es einmalig gegen den Spiegel aus; nach
  Schema-Upgrades GRANT erneuern).
- **Kompatibilitätsdetail:** Sekunden aus `length`-Millisekunden per
  Integer-Division abschneiden (nicht runden).

### SQLite (Wächter, Cache)
| Tabelle | Inhalt |
|---|---|
| `api_key` | Key (Hash), Label, aktiv, erstellt, zuletzt benutzt |
| `admin_user` | Login, Passwort-Hash (argon2) |
| `update_run` | Historie der Job-Läufe: Art, Start, Ende, eingespielte Dateien, Zeilen, Ergebnis, Fehlermeldung. Seit M2.5 kennt `kind` die sechs Arten aus E10 (`acoustid-delta`, `discogs-dump`, `caa-crawl`, `nachzuegler`, `backup`, `queue-send`); aus `update` wurde `acoustid-delta` |
| `event_log` | Ereignisse (Start/Stopp, Wecken, Fehler, Notifications) mit Level und Zeitstempel, ringpuffer-artig begrenzt |
| Lookup-Cache | **Eigene SQLite-Datei** `lookup-cache.sqlite3` neben der Zustandsdatenbank (Phase 17, nicht in dieser Datenbank — Massenschreibvorgänge); Schlüssel = SHA-256 über Pfad und alle Anfrageparameter außer `client`/`clientversion`, Wert = die rohe Antwort (Status, Kopfzeilen ohne `date`/`content-length`, Rumpf); Verdrängung nach LRU bis 90 % von `cache.max_size_mb`; invalidiert nach Delta-Import und nach lokaler Submission |

### config.yaml (Wächter, Cache)
Alle Laufzeit-Einstellungen (siehe §6). Vom Wächter gelesen/geschrieben;
der API-Dienst erhält die relevante Teilmenge beim Start bzw. per
Reload-Signal vom Wächter. Das Reload-Signal ist eine Markierungsdatei
`config.yaml.reload` neben der Konfiguration (JSON mit monoton
wachsendem Zähler, atomar geschrieben; Sendeseite Phase 14,
`watchdog/app/reload.py`) — beide Prozesse sehen dieselbe Datei auf dem
`/config`-Mount. Empfangsseite seit Phase 15: `api/app/reload.py` prüft
alle 10 s und übernimmt die zur Anfragezeit gelesene Teilmenge
(`acoustid.submit.mode`, `acoustid.submit.upstream_app_key`,
`mb.keep_submitted_mbid`); `acoustid.index.query_hashes` und `mb.dsn`
werden bewusst nicht übernommen, sondern auf den laufenden Wert
zurückgeschrieben und als Warnung geloggt.

## 6. Konfiguration — Schlüssel, Defaults, feste Werte

Laufzeit-Einstellungen in `config.yaml` auf `/config` (Cache-Mount des
Wächters, editierbar über die Admin-UI). Env-Variablen (Prefix `MMO_`) nur
für Bootstrap (Pfade, Ports, DB-/Index-/API-Adressen) — verbindliches
Schema: `shared/shared/env.py`, Vorlage `.env.example`.

**Umbenennung der Schlüssel in M2 (E9).** Mit der Scope-Erweiterung bekam
der AcoustID-Teil einen eigenen Ast, und der Plattenplatz-Guard wurde
quellenneutral (gemessen wird ein Dateisystem, nicht eine Quelle):

| bis M1 | seit M2 |
|---|---|
| `submit.mode`, `submit.upstream_app_key` | `acoustid.submit.*` |
| `update.time` | `acoustid.update.time` |
| `update.min_free_gb` (Default 50) | `disk.min_free_gb` (Default **100**) |
| `index.query_hashes` | `acoustid.index.query_hashes` |

Die alten Pfade werden **eine Release-Runde** weitergelesen — der neue Pfad
gewinnt, jeder Fund erzeugt eine Warnung mit dem neuen Namen, und der
Wächter schreibt die Datei beim ersten Start einmalig auf das neue Schema
um (`shared.config.migrate_legacy_keys`, `acoustid_watchdog.config_store`).
Ohne diesen Übergang wäre ein bestehendes `submit.mode: off` kommentarlos
auf den Default `local` zurückgefallen und die Instanz hätte Einreichungen
angenommen, die der Betreiber abgeschaltet hatte — stille Config-Amnesie,
Risiko R7 der M0-Analyse. Dieselbe Regel gilt für den Env-Prefix
(`AOFF_` → `MMO_`, E5).

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `auth.mode` | `none` | `none` \| `apikey` |
| `auth.allow_known_client_keys` | `false` | `apikey`-Modus: fest einkodierte Keys bekannter Drittclients (Picard `v8pQ6oyB`, beets `1vOwZtEn`) zulassen — bewusst default aus, da öffentlich bekannt |
| `ratelimit.per_ip_per_min` | `120` | Anfragen pro IP pro Minute |
| `acoustid.submit.mode` | `local` | `off` \| `local` \| `local+upstream` |
| `acoustid.submit.upstream_app_key` | leer | Application-Key für api.acoustid.org (Secret) |
| `acoustid.update.time` | `04:00` | Täglicher Delta-Import (lokale Zeit) |
| `acoustid.index.query_hashes` | `120` | Query-Hashes je Fingerprint im Suchindex; RAM-abhängig pro Host einstellbar (z. B. 80 bei wenig RAM). Änderung erfordert Index-Neuaufbau; Empfehlungstabelle entsteht aus dem Probelauf |
| `disk.min_free_gb` | `100` | Mindest-Plattenreserve vor jedem Import-/Crawl-Segment (gelesen als GiB — strengere Lesart; `0` schaltet den Guard ab). **Ein** Grenzwert, aber geprüft gegen **jeden** Schreib-/Staging-Pfad: die Mounts aus §3 sind mehrere Dateisysteme, und ein freies `/import` sagt nichts über `/data/db` (E11). Seit M2.5 prüft der Wächter vor jedem Job `/import`, `/data/db`, `/config` und `backup.dir` (je Dateisystem einmal); der Importer misst zusätzlich laufend sein Dump-Verzeichnis |
| `wake.hold_timeout_s` | `90` | Max. Haltezeit einer Anfrage beim Wecken |
| `idle.timeout_min` | `15` | Auto-Stopp nach Inaktivität |
| `cache.enabled` | `true` | Lookup-Cache an/aus |
| `cache.max_size_mb` | `512` | Obergrenze Lookup-Cache |
| `metrics.enabled` | `false` | Prometheus-Endpoint |
| `notify.ntfy.url` | leer | ntfy/Webhook-Ziel (leer = aus) |
| `notify.smtp.*` | leer, `port` `587` | `host`, `port`, `user`, `pass`, `from`, `to` (leerer Host = aus) |
| `backup.dir` | leer | Backup-Ziel (leer = Backup aus) |
| `backup.time` | `04:45` | Backup nach dem Update-Lauf |
| `backup.include_covers` | `false` | Cover mitsichern (v2 §6.12). Aus, weil die Bilder aus den Quellen rekonstruierbar sind — anders als `local_submission`, das es nirgends sonst gibt |
| `mb.dsn` | leer | Read-only-DSN der MusicBrainz-Postgres (Secret) |
| `mb.keep_submitted_mbid` | `false` | Redirect-Auflösung (§5.4): `false` = Antwort trägt die kanonische MBID, `true` = die eingereichte wird durchgereicht (Phase 10) |

**Platzhalter der Scope-Erweiterung (v2 §7, seit M2 im Schema).** Diese
Schlüssel stehen ab M2 in der `config.yaml`, damit ein Betreiber seine
Zugänge schon hinterlegen kann; die auswertende Fachlogik kommt mit M3–M6,
die Eingabefelder dafür mit der Admin-UI in M8. Bis dahin sind es reine
Trägerwerte — alle so vorbelegt, dass die Quelle **aus** ist (v2 §2
„Repo-Defaults sind konservativ"):

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `discogs.update.check_time` | `05:00` | Täglicher Check auf einen neuen Monats-Dump (M3) |
| `discogs.token` | leer | Discogs-API-Token für die Bilder-API (Secret); leer = Discogs-Bildquelle aus |
| `tadb.api_key` | leer | TheAudioDB-Key (Secret); leer = Quelle aus (M6) |
| `caa.crawl.enabled` | `false` | Voll-Spiegel-Crawler des Cover Art Archive (M5). Default aus: ein Erst-Crawl läuft Wochen und hält das Array so lange wach — das ist eine Betreiber-Entscheidung, keine Repo-Vorgabe |
| `caa.crawl.rate_per_s` | `2` | Crawler-Drossel; gilt auch für Lazy-Abrufe derselben Queue |
| `covers.negative_retry_days` | `30` | Wiederholung, wenn keine der drei Quellen ein Cover hatte (M4) |

**Umsetzung (M2.5).** `notify.*`, `backup.*` und `metrics.enabled` werden
seit M2.5 wirklich ausgewertet:

- **`notify.*`** (`watchdog/app/notify.py`): zwei Kanäle, beide per Default
  aus. `notify.ntfy.url` ist ein `POST` mit dem Meldungstext als Rumpf und
  Titel/Dringlichkeit in Kopfzeilen — das ntfy-Protokoll, und für einen
  beliebigen Webhook bleibt es ein POST mit lesbarem Text. `notify.smtp.*`
  nutzt STARTTLS (implizites TLS auf Port 465), Anmeldung nur mit
  gesetztem Benutzer. Fünf Anlässe: Import fehlgeschlagen, Plattenplatz
  knapp, Stack-Start-Fehler, `upstream_forward_gave_up`, Versions-Drift
  (E14) — dazu eine Testnachricht je Kanal. Ein Zustellfehler ist eine
  Warnung im Ereignis-Log und bricht nie einen Lauf ab.
- **`backup.*`** (`importer/app/backup.py`, K9): `backup.time` löst einen
  Job aus, der `local_submission`, die Wächter-SQLite und die
  `config.yaml` nach `backup.dir` sichert — **ohne** den Lookup-Cache.
  `backup.include_covers` steht im Schema, hat aber bis M4 keine Wirkung.
  Wiederherstellung: [docs/backup-restore.md](docs/backup-restore.md).
- **`metrics.enabled`**: siehe `GET /metrics` in §7.

Die Termine werden bei **jeder** Fälligkeitsprüfung frisch gelesen, die
Benachrichtigungskanäle bei jedem Versand — eine Änderung über die
Admin-UI wirkt ohne Neustart (dasselbe Muster wie im Proxy und im
Idle-Stopp).

**Secrets** (`acoustid.submit.upstream_app_key`, `notify.smtp.pass`,
`mb.dsn`, `discogs.token`, `tadb.api_key`) sind `SecretStr`: in `repr()`,
`str()` und Logs maskiert, im Klartext nur beim Schreiben der Datei. Die
`config.yaml` bekommt Modus **0640** — nicht 0600, weil der API-Dienst sie
lesen muss und seit M1b unprivilegiert läuft; die Gruppe trägt im Container
genau diesen Dienst.

**Feste Werte:**
- **Port:** ein veröffentlichter Port (Default `8080`, `MMO_PORT`) für
  API-Proxy und Admin-UI unter `/admin`.
- **Container und Prozesse:** ein Container (heißt `<projekt>-app-1` —
  einen festen `container_name` vergibt die Compose-Datei bewusst nicht)
  mit den vier supervisord-Prozessen `watchdog`, `index`, `db`, `api`
  (§3).
- **Interne Adressen (Bootstrap-Werte, keine Codekonstanten):**
  `MMO_API_BASE_URL` (Default `http://127.0.0.1:8081`) ist das Ziel des
  Proxys, `MMO_API_HEALTH_URL` die Bereitschaftsfrage des Weckens (folgt
  der Basis-URL, wenn nicht gesetzt), `MMO_API_PORT` (Default `8081`) der
  Lauschport des API-Dienstes; `MMO_INDEX_URL` (Default
  `http://127.0.0.1:6081`) und `MMO_INDEX_NAME` (Default `main`) der
  Suchindex, `MMO_DB_*` die Datenbank. Das DB-Passwort steht in
  `MMO_DB_PASSWORD_FILE` (Default `/config/db-password`) und wird beim
  ersten Start vom Entrypoint erzeugt (E16).
- **Batch-Limit:** max. 100 Einträge pro `/v2/lookup/batch`-Request.
- **Upstream-Retries:** nach 7 Fehlversuchen Notification + manueller
  Retry über die Admin-UI.
- **Idle-Definition:** keine API-Anfrage im Timeout-Fenster UND kein
  laufender Import-/Backup-Job (ab M5 zusätzlich: Crawler inaktiv).
- **Admin-Login:** ein Benutzer; Passwort-Hash (argon2) in der SQLite;
  Erst-Passwort beim ersten Start generiert und ins Containerlog
  geschrieben (der Weg dorthin ist `docker compose logs app`).
- **UI-Polling:** Statuskarte und laufende Jobs 5 s (HTMX); statische
  Seiten pollen nicht.

## 7. API-Spezifikation

### Kompatibel zu api.acoustid.org
- **`GET/POST /v2/lookup`** — Parameter `client`, `fingerprint`,
  `duration`, `meta` (u. a. `recordings`, `releasegroups`, `compress`
  gemäß Original). Antwortformat identisch zum Original (JSON, `status`,
  `results[]` mit `id`, `score`, optional `recordings[]`). Im Modus
  `apikey` wird `client` gegen die Key-Liste geprüft; im Modus `none`
  wird `client` ignoriert, aber akzeptiert.
- **`POST /v2/submit`** — Parameter gemäß Original (`client`, `user`,
  `fingerprint.N`, `duration.N`, MBID/Metadaten-Felder). Verhalten je
  nach `acoustid.submit.mode`; Antwortformat identisch zum Original.

### Kompatibilitätsvertrag (verifiziert in Phase 1)

Details: [docs/research/phase1-api-formate.md](docs/research/phase1-api-formate.md).
Kernpunkte: GET **und** POST überall; Parameter aus Query-String +
form-urlencodetem Body (kein JSON-Body); gzip-Request-Bodys entpacken;
1-MiB-Limit → Fehler 19/HTTP 413 (Picard stützt sein Batching darauf);
Chromaprint-Base64 (URL-safe, ohne Padding, nur Version 1);
`format=json|xml|jsonp`; `meta`-Werte mit Präzedenzregel (m2 >
recordings > releasegroups > releases), `sources` nötig für Picards
Ranking; Score-Semantik: nur > 0,4, absteigend, max. 10, dedupliziert;
Limits 20 Fingerprint-/100 Track-Queries; **Original-Batchprotokoll
(`fingerprint.N` + `batch=1`) wird zusätzlich zum eigenen
Batch-Endpoint unterstützt**; Fehlerformat
`{"status":"error","error":{"code":<int>,"message":"<str>"}}` mit den
19 Original-Codes (u. a. 4→400, 14→429, 19→413, 13→503, 5→500);
`Access-Control-Allow-Origin: *`. Korrektur zum Handoff: der
Status-Endpoint heißt **`/v2/submission_status`** (Mehrfach-`id`;
unbekannte IDs bleiben `"pending"`, nie 404). Submit: `status` immer
`"pending"`, `index` als String nur bei `.N`-Suffix, mehrfaches
`mbid.N` erzeugt mehrere Submissions.

**Upstream-Weiterleitung (`local+upstream`):** eigener
Application-Key als `client` (Registrierung acoustid.org/new-application,
sofort aktiv); `user`-Key des einreichenden Clients unverändert
durchreichen (Zweckbindung); hart ≤ 3 req/s drosseln; kein
`Retry-After` upstream → eigenes Backoff (exponentiell ab 1 s, Deckel
30 s, persistente Queue); nur https.

### Eigene Endpoints
- **`POST /v2/lookup/batch`** — JSON-Body: Objekt-Hülle
  `{"client", "meta", "maxdurationdiff", "queries": [{fingerprint,
  duration, meta}, …]}` (Hülle statt nacktem Array — erweiterbar ohne
  Vertragsbruch, Phase-13-Entscheid); Antwort
  `{"status":"ok","responses":[…]}` in Anfragereihenfolge, je Eintrag
  eine vollständige AcoustID-Antwort mit `index` (0-basiert) —
  Teilfehler einzelner Einträge bei HTTP 200. Obergrenze 100 Einträge
  ⇒ 19/413. Details: [docs/api-lookup.md](docs/api-lookup.md).
  (Zusätzlich gilt das Original-Batchprotokoll mit max. 20, s. o.)
- **`GET /status`** — Wächter-Endpoint, weckt nie: Zustand
  (schlafend/startend/bereit/stoppt/Fehler), Datenstand (letzte
  Delta-Sequenz), letzter Update-Lauf, Version. Seit M2 zusätzlich
  `components` mit den **eingebackenen** Fremdkomponenten dieses
  Artefakts (`postgresql_major`, `acoustid_index_commit`; v2 §12) —
  wer den Versions-Drift-Guard (E14) debuggt, muss die Major sehen
  können, ohne an das Image heranzukommen. **Erweiterungen sind
  ausschließlich additiv:** das Feld `stack` behält Namen und Form,
  auch wenn im Ein-Container-Betrieb kein Stack im Wortsinn mehr
  existiert — Container-Healthcheck und Betreiber-Skripte hängen daran
  (E16). Der Endpunkt ist bewusst offen (kein Key, kein Rate-Limit).
- **`GET /_health`** (API-Dienst, **intern**, kein Vertragsteil,
  Phase 15) — Bereitschaftsprüfung für das Wake-on-request des
  Wächters: DB (`SELECT 1`) + Index (`/<name>/_health`), bewusst ohne
  MusicBrainz (§8.7); erreichbar nur containerintern (der Dienst hat
  keinen veröffentlichten Port, und der Proxy reicht nur `/v2/*`
  weiter — der Pfad wird zusätzlich abgewiesen).
- **`GET /metrics`** — Prometheus-Format, nur wenn `metrics.enabled`
  (Umsetzung M2.5, `watchdog/app/metrics.py`): Lookups, Cache-Quote,
  Weckvorgänge, Prozess-Zustand, Läufe und Laufdauern je Art. Wie
  `/status` **offen und weckfrei** — der Prozess-Zustand kommt aus der
  Momentaufnahme des Zustandsabgleichs, nicht aus einer eigenen Abfrage
  an supervisord. Abgeschaltet antwortet der Pfad mit **404**, nicht 403:
  der Wächter gibt nicht preis, dass es den Endpunkt gibt.
- **`/admin/...`** — Admin-UI (server-rendered), Passwort-geschützt.

### Umsetzungsstand

`GET/POST /v2/lookup` steht seit Phase 9 vollständig, seit Phase 10
**inklusive `meta`** (MB-Resolver `shared/shared/mb/` + `api/app/meta.py`;
volle Grammatik mit Original-Präzedenz, degradierter Betrieb nach §8.7,
Abweichungen tabelliert in [docs/api-lookup.md](docs/api-lookup.md)).
Seit Phase 11 steht `GET/POST /v2/submit` in den Modi `off`/`local`
(`api/app/submit.py`; `local_submission` §5.2, reservierter
Doc-ID-Bereich §5.3), seit Phase 12 auch `local+upstream`
(`api/app/upstream.py`: Erstversuch in der Anfrage, Queue-Drain für
den Update-Lauf, Drossel ≤ 3 req/s, 7-Fehler-Grenze §8.9; Vertrag und
Abweichungen: [docs/api-submit.md](docs/api-submit.md)). Seit
Phase 13 stehen `POST /v2/lookup/batch` (`api/app/batch.py`) und
`GET/POST /v2/submission_status` (`api/app/status.py`; Mapping:
`new` ⇒ `"pending"`, ab `indexed` ⇒ `"imported"` mit `result.id` =
lokale AcoustID). **Der API-Block (Phasen 9–13) ist damit
vollständig.**

Seit M2.5 kommt `GET /metrics` dazu (`watchdog/app/metrics.py`, nur bei
`metrics.enabled`), und der API-Dienst bekommt einen zweiten Job an die
Seite: `python -m acoustid_api.queuejob` arbeitet die Upstream-Warte-
schlange als eigener Prozess ab (E10 — der Wächter darf sie anstoßen,
aber nicht selbst ausführen: er hält keine Verbindung zum Array).

### Lookup-Cache (Phase 17)
Der Wächter beantwortet ein wiederholtes `GET`/`POST /v2/lookup` aus seiner
eigenen Cache-Datei — **ohne** Docker-Kontakt, ohne API-Kontakt und ohne
Weckvorgang (Invariante §8.2 baulich: der Cache-Zweig liegt vor dem
Wecken). Eingelagert werden nur Antworten mit HTTP 200 **und** JSON-Rumpf
mit `status: "ok"`; `format=xml`/`jsonp` fallen damit heraus, ebenso jede
Fehlerantwort. `POST /v2/lookup/batch` wird bewusst **nicht** gecacht
(Teilfehler stehen dort *innerhalb* einer 200er-Antwort, Phase 13). Eine
Antwort aus dem Cache ist bytegleich zur ursprünglichen — kein
`X-Cache`-Vermerk, kein Unterschied zwischen „mit Cache" und „ohne".
**Ein Treffer zählt nicht als Aktivität** (§6 „Idle-Definition"): er
braucht das Array nicht, ein Stack, den nur noch Treffer erreichen, darf
einschlafen. Ein defekter Cache wird weggeworfen und neu angelegt; er hält
nichts, was sich nicht neu berechnen ließe.

### Durchsetzungsort Auth & Rate-Limit
API-Key-Prüfung (`apikey`-Modus) und IP-Rate-Limit setzt der **Wächter**
am Proxy durch — auch für Cache-Hits bei schlafendem Stack (Entscheid
2026-07-25, siehe DECISIONS.md). Der API-Service selbst prüft keine Keys.

**Umsetzung (Phase 18).** Die Reihenfolge im Proxy-Pfad ist von außen nach
innen gebaut: **Rate-Limit → Auth → Cache → Wecken/Weiterleiten**
(`watchdog/app/main.py`). Beide Wächter am Eingang arbeiten ausschließlich
aus Wächter-Daten und können den Stack gar nicht anfassen — Invariante §8.2
gilt damit auch für jede abgewiesene Anfrage.

- **`apikey`:** `client` wird wie in der API gelesen (Query-String vor
  Form-Rumpf, gzip entpackt; bei `/v2/lookup/batch` zusätzlich aus der
  JSON-Hülle) und gegen `api_key` geprüft — nur aktive Keys, Vergleich über
  einen ungesalzenen `sha256`-Hash (die Keys sind selbst erzeugte
  Zufallswerte; ein KDF je Anfrage wäre unangemessen und machte die
  Nachschlagbarkeit unmöglich). `last_used_at` wird gedrosselt geschrieben
  (höchstens einmal je Minute und Key).
- **`none`:** `client` wird akzeptiert und ignoriert; ob er fehlt,
  entscheidet weiterhin die API (Fehler 2).
- **Rate-Limit:** gleitendes Minutenfenster je **direkter** Client-IP,
  aktiv in beiden Modi. `X-Forwarded-For` wird bewusst **nicht** ausgewertet
  (offener Klärungspunkt für den Betrieb hinter einem TLS-Proxy).
- **`/status` bleibt offen** — ohne Key und ohne Limit: es ist zugleich
  Bereitschaftsanzeige, Container-Healthcheck und Datenquelle der
  Admin-Statuskarte.

### Fehlerverhalten
- Aufwecken: Anfrage wird gehalten; erst nach `wake.hold_timeout_s`
  ohne Bereitschaft → `503` mit `Retry-After`.
- Stack-Start-Fehler → `503` + Notification.
- Ungültiger/fehlender Key im `apikey`-Modus → Fehlerantwort im
  AcoustID-Fehlerformat.
- Rate-Limit überschritten → `429` + `Retry-After`.

**Eigene Fehlerantworten des Wächters** tragen die Codes und die Wortlaute
der Original-Fehlertabelle (Phase 18): 2/400 `missing required parameter
"client"`, 4/400 `invalid API key`, 13/503 `service currently unavailable,
try again later`, 14/429 `rate limit (…) exceeded, try again later`, 19/413
`request too large`. Sie nennen **keine internen Details** (Containernamen,
interne URLs) — der Grund steht im Container- und im Ereignis-Log.
Bewusste Abweichungen vom Original: `Retry-After` bei 503 und 429 (das
Original schickt den Kopf nie; §7 „Fehlerverhalten" verlangt ihn) und die
Rate in der 14er-Meldung, die aus `ratelimit.per_ip_per_min` in
Anfragen/Sekunde umgerechnet wird. Eigene Fehlerantworten sind immer JSON,
auch bei `format=xml|jsonp` — der Wächter baut keine zweite
Format-Schicht.

## 8. Verhaltensregeln & Invarianten

1. **Der Wächter weckt, sonst niemand.** Nur der Wächter startet und
   stoppt die schlafenden Prozesse — über den supervisord-Socket, nicht
   über Docker: **docker.sock ist ersatzlos entfallen** (E1). Präzisierung
   aus E10: **Dauerdienste** (`db`, `api`, `index`) laufen unter
   supervisord, **Jobs** (Importer, Backup, ab M3 Dump-Import und
   Crawler) sind direkte Subprozesse des Wächters. API und Jobs steuern
   selbst nie Prozesse.
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
   Delta-Import und nach jeder lokalen Submission vollständig invalidiert.
7. **Degradierter Betrieb bei MB-Ausfall.** Ist die MB-Postgres nicht
   erreichbar, liefert Lookup AcoustID-UUIDs + MBIDs ohne Metadaten
   (kein Fehler); Ereignis wird geloggt.
8. **Plattenplatz-Guard.** Vor jedem Import-/Crawl-Segment: freier Platz ≥
   `disk.min_free_gb`, sonst Abbruch + Notification. Geprüft wird
   **jeder** Schreib-/Staging-Pfad, nicht nur einer (E11): der Wächter
   misst vor jedem Job `/import`, `/data/db`, `/config`, `/index` und
   `backup.dir` (je Dateisystem einmal, `watchdog/app/diskspace.py`), der
   Importer zusätzlich laufend sein Dump-Verzeichnis. Eine Unterschreitung
   bricht den Lauf ab, **bevor** die Prozesse geweckt werden — er steht
   dann als `aborted` in der Historie.
9. **Upstream-Queue.** Fehlgeschlagene Upstream-Submits bleiben in
   `local_submission` (`forward_failed`) und werden beim nächsten
   Update-Lauf erneut versucht; nach 7 Fehlversuchen Notification und
   manueller Retry über die Admin-UI. Seit M2.5 läuft dafür direkt nach
   dem Delta-Import der Job `queue-send` (`api/app/queuejob.py`) — als
   eigener Eintrag in der Historie, im selben wachen Fenster.
10. **Secrets nie im Repo.** Alle Zugänge über `.env`/`config.yaml`;
    `.env.example` dokumentiert alles.
11. **Ein Release = ein Image = ein Tag.** Seit dem Ein-Container-Umbau
    gibt es genau ein Artefakt; `release.yml` baut es aus einem
    SemVer-Tag und schiebt es nach GHCR.
12. **Submits während des Update-Laufs werden zurückgestellt** (Entscheid
    2026-08-05, M2.5). Sie werden angenommen und gespeichert (Status
    `new`), aber erst **nach** dem Lauf indexiert: eine Indexierung
    dazwischen erhöhte die Index-Version, und der Feed des Importers
    bräche an seinem `expected_version`-Guard ab — ein ganzer Tag
    Datenstand für eine Einreichung, die eine Minute später genauso
    sichtbar wird. Die Antwort bleibt `pending`, also unverändert.
    Umgesetzt über eine Marke auf `/config` (`index-feed.busy`);
    nachgetragen wird im `queue-send`-Job, unabhängig vom Submit-Modus.
    Die Marke trägt ihren Setzzeitpunkt und **läuft nach 24 Stunden ab** —
    nach einem harten Prozessende bliebe sie sonst für immer liegen. Sie
    schützt nur wächtergesteuerte Läufe; ein von Hand gestarteter
    Bootstrap hat sie nicht, deshalb heilt der Index-Feed einen
    vereinzelten Versionskonflikt selbst (zwei Wiederholungen mit frischer
    Version, danach harter Abbruch — ein echter zweiter Importer fällt
    weiterhin auf).
13. **Genau ein Job gleichzeitig.** Zwei Importer nebeneinander kämen sich
    in `import_state` ins Gehege, zwei Sicherungen schrieben in dasselbe
    Verzeichnis. Der Job-Manager des Wächters lehnt den zweiten ab; der
    nächste Takt des Zeitplans fragt wieder.

## 9. Admin-UI (Referenz: docs/DESIGN_HANDOFF.md)

- **Technischer Rahmen (fix):** FastAPI + Jinja2 + HTMX im
  Wächter-Prozess unter `/admin`; ein Admin-Benutzer, Passwort-Login,
  Session-Cookie; responsive (Desktop primär, Tablet/Smartphone
  benutzbar); CSS ohne Build-Schritt; kein WebSocket.
- **Routen:** `/admin/login`, `/admin/` (Dashboard), `/admin/config`,
  `/admin/keys`, `/admin/jobs`, `/admin/logs`, `/admin/stats`.
- **Stack-Zustände** (überall ablesbar): `schlafend` (neutral — guter
  Zustand!), `startet`, `bereit`, `stoppt`, `fehler`. Zusätzliche Badges:
  Import läuft (Datei x von y), Backup läuft, Upstream-Queue: N,
  MB nicht erreichbar, Plattenplatz knapp.
- **Interaktionsprinzipien:** weckende/destruktive Aktionen immer mit
  Bestätigung + Kennzeichnung („weckt das Array"); zustandsabhängige
  Buttons; Fehler laut, Erfolg leise; Secrets nach dem Speichern nie im
  Klartext; UI-Sprache Deutsch (Fachbegriffe englisch).
- Visuelles Design entsteht in separater Claude-Design-Session auf Basis
  des DESIGN_HANDOFF; Screen-Details siehe dort (§4).

## 10. Filestruktur (Repo)

```
musicmeta-offline/
├── Dockerfile                    # das eine Image: App + PostgreSQL + acoustid-index
├── docker-compose.yml            # der eine Service (Bind-Mounts, Healthcheck /status)
├── .env.example                  # Alle Bootstrap-Env-Variablen (MMO_*), dokumentiert
├── README.md                     # Setup Unraid + generisch, Bootstrap-Anleitung, Lizenzhinweis Daten
├── THIRD-PARTY-NOTICES.md        # GPL-Pflichten des eingebackenen fpindex (E7)
├── supervisor/                   # supervisord.conf (+ .dev), entrypoint.sh,
│                                 #   mmo-postgres / mmo-fpindex (Startskripte)
├── unraid/                       # Unraid-Community-App-Template (XML)
├── watchdog/
│   └── app/                      # Module: Proxy, Weck-/Prozess-Steuerung, Scheduler,
│       ├── ...                   #   Admin-Routen, Auth, Lookup-Cache, Notify, Metrics
│       ├── templates/            # Jinja2-Templates der Admin-UI
│       └── static/               # CSS/JS (HTMX) der Admin-UI, kein Build
├── api/
│   └── app/                      # /v2/lookup, /v2/submit, /v2/lookup/batch,
│                                 #   /v2/submission_status, Index-Client, MB-Resolver
├── importer/
│   └── app/                      # Delta-Download, Parser, DB-Import,
│                                 #   Index-Feed, Backup-Job
├── shared/                       # Python-Paket: Config-/Env-Schema, Modelle, Logging,
│                                 #   DB-Migrationen, Index-Client, Fingerprint-Codec, MB-Queries
├── docs/
│   ├── HANDOFF.md                # Gesamtspezifikation v2 (Quelle dieser Datei)
│   ├── DESIGN_HANDOFF.md         # UI-Spezifikation (noch v1; v2 kommt zu M8)
│   ├── migration-v1-v2.md        # Volume-Migration aus dem v1-Stack
│   ├── backup-restore.md         # Was gesichert wird und wie man es zurückspielt (K9)
│   └── ...                       # api-lookup/api-submit, importer-job, probelauf-unraid,
│                                 #   design/, research/, archive/
├── tests/                        # paketübergreifende Tests, Fixtures, pg_acoustid
└── .github/workflows/            # ci.yml (Lint/Tests/Image), release.yml (ein Image → GHCR)
```

Es gibt **kein** `docker-compose.watchdog.yml` und keine drei Dockerfiles
mehr; `tests/test_repo_layout.py` hält beides fest, damit eine alte
Anleitung sie nicht versehentlich zurückbringt.

Der Dateischnitt unterhalb der `app/`-Verzeichnisse ist im Handoff nicht
festgelegt und wird in den jeweiligen Phasen konkretisiert (dann hier
nachtragen).

**Paketierung (Phase 2, fortgeschrieben in M2):** uv-Workspace, Python
3.14, ruff + pytest. Verzeichnisse wie oben; installierte Import-Namen
weichen ab: `acoustid_api`, `acoustid_importer`, `acoustid_watchdog`,
`shared` (Details/Begründung in DECISIONS 2026-07-25), die
Distributionsnamen tragen seit M2 den neuen Projektnamen
(`musicmeta-offline-api`, `-importer`, `-watchdog`, `-shared`). Ab M3
kommen die neuen Subsysteme als eigene Workspace-Member dazu
(`mmo_discogs_dump`, `mmo_covers`, `mmo_tadb`, `mmo_mbref`, E6). Echte
Dump-Fixtures sind nicht committet —
`tests/fixtures/fetch_fixtures.py` beschafft sie reproduzierbar.
Repo: https://github.com/shares92/musicmeta-offline (Reihenfolge der
Umbenennung: README „Umbenennung: acoustid-offline → musicmeta-offline").

## 11. Bewusst ausgeschlossen

- Kein eigener MusicBrainz-Spiegel (extern vorausgesetzt; bei
  Nichterreichbarkeit degradierter Betrieb).
- Kein Fingerprint-Berechnen serverseitig.
- Keine Volltext-/Metadaten-Suche, kein Browsing des Datenbestands.
- Keine Mehrbenutzer-/Rollenverwaltung in der Admin-UI (ein Admin-Login).
- Kein Kubernetes/Helm — Docker Compose only.
- Keine Weiterverteilung des Datenbestands (Lizenzthema beim Betreiber).

## 12. Offene Recherchepunkte & Risiken

Offene Punkte:
1. ~~JSON-Delta-Format~~ — erledigt Phase 0 (§5).
2. ~~Bootstrap-Strategie~~ — entschieden 2026-07-25 (DECISIONS);
   Dauer wird per Probelauf in Phase 8 gemessen (nirgends existiert
   eine belegte E2E-Importdauer).
3. ~~acoustid-index~~ — erledigt Phase 1 (§5.3, DECISIONS: Cache-Pool,
   zweistufiges Rescoring, `acoustid.index.query_hashes`; aus dem
   Digest-Pin wurde mit M1b ein Quell-/Commit-Pin, E7).
4. ~~Upstream-Submit~~ — erledigt Phase 1 (§7,
   docs/research/phase1-api-formate.md).
5. ~~MB-Schema~~ — erledigt Phase 1 (§5.4,
   docs/research/phase1-mb-schema.md).
6. ~~Auth-Default `none`~~ — bestätigt 2026-07-25 (DECISIONS).
7. Daten-Flaute seit 2026-07-22 (Export-Pipeline läuft, Inhalte fast
   leer; keine offizielle Ankündigung) — vor Produktivstart erneut
   prüfen.
8. Exporter-Codestand (2023) vs. laufende Produktion: Feldsatz sehr
   wahrscheinlich unverändert — der Importer bekommt trotzdem einen
   Feld-Sanity-Check je Strom (unbekanntes Feld ⇒ Warnung).
9. Umfang der Vor-2011-Lücke nicht bestimmbar (akzeptiert, §5.1);
   optional bei AcoustID OÜ nachfragen — ebenso Fair-Use-Absprache vor
   dem 414-GB-Vollabzug.
10. acoustid-index `ng` beobachten (wire-kompatibel, aber
    Index-Neuaufbau beim Umstieg; kein Release-Datum).
11. Reale Index-Größe und RAM-Empfehlungstabelle je
    `acoustid.index.query_hashes`-Wert: aus dem Probelauf ableiten.
12. Index-Restore-Prozedur (`_snapshot` → tar) einmal manuell testen
    und dokumentieren (kein Restore-Endpoint; `manifest.backup` wird
    vom Code nie gelesen).
13. Picard-Umbiegung auf unsere Instanz (URL hart kodiert):
    Quelltext-Patch vs. Plugin-Monkeypatch vs. DNS — Entscheid beim
    Go für Phase 28; beets ist offiziell umbiegbar
    (`acoustid.set_base_url`).
14. Score-Parität mit acoustid.org (fpstore-Formel nicht öffentlich):
    optionaler empirischer Abgleich mit Test-Fingerprints gegen die
    öffentliche API.
15. ~~Config-Flag für MBID-Redirect-Durchreichung~~ — erledigt Phase 10:
    `mb.keep_submitted_mbid` steht in §6, im Schema und in DECISIONS
    (E16 bestätigt ihn für v2).
16. Der pg_acoustid-Test-Container (CI-Bit-Verifikation, Phase 9) baut
    den Extension-Quelltext zur Bauzeit aus dem Upstream-Repo (letzter
    Commit 2021, per SHA gepinnt). Verschwindet das Repo, bricht der
    CI-Job — dann Quelltext-Tarball selbst spiegeln (lizenzrechtlich
    nur privat halten, pg_acoustid hat keine Lizenzdatei).

Risiken (Priorität aus dem Handoff):
1. **Dump-Format/Bootstrap (hoch)** — größtes Projektrisiko, zuerst
   verifizieren, bevor weitere Phasen starten.
2. acoustid-index-Unbekannte (mittel).
3. Postgres auf Spindeln (mittel) — mitigiert durch Stale-Serving +
   nächtliches Zeitfenster.
4. MB-Schema-Kopplung (niedrig) — mitigiert durch Query-Schicht +
   degradierten Betrieb.
5. ~~docker.sock im Wächter (akzeptiert)~~ — **entfallen mit M1b (E1):**
   der Wächter steuert die Prozesse über den supervisord-Socket, es gibt
   gar keinen Docker-Zugriff mehr aus dem Container (§8.1). Die alte
   Mitigation („minimaler Codepfad") ist damit gegenstandslos;
   `tests/test_repo_layout.py::test_no_docker_socket_anywhere_in_the_app`
   hält fest, dass der Pfad nicht zurückkehrt.
6. Lizenz CC BY-SA 3.0 (niedrig) — README-Hinweis bei Weitergabe.
