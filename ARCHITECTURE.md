# ARCHITECTURE.md — acoustid-offline

Statische technische Referenz. Quelle: [docs/HANDOFF.md](docs/HANDOFF.md) (Gesamtspezifikation)
und [docs/DESIGN_HANDOFF.md](docs/DESIGN_HANDOFF.md) (Admin-UI). Bei Widerspruch gilt das Handoff.
Änderungen an dieser Datei nur mit DECISIONS.md-Eintrag.

Stand: 2026-07-25 (aus Architektur-Session; exaktes DB-Spaltenschema folgt aus Phase 0).

---

## 1. Zielsetzung

Selbst gehostete, offline-fähige AcoustID-Instanz als Docker-Stack:
Audio-Fingerprint-Lookup (Chromaprint → AcoustID-UUID → MusicBrainz-Recording
inkl. Metadaten) ohne Abhängigkeit vom öffentlichen api.acoustid.org.

**Erfolgskriterien:**
1. Standard-Clients (primär DroppedNeedle, außerdem Picard/beets per
   URL-Umbiegung) bekommen auf `/v2/lookup` korrekte, API-kompatible Antworten.
2. Der Stack schläft im Normalzustand vollständig (Array-Platten dürfen
   herunterfahren); nur der Wächter läuft dauerhaft auf dem Cache.
3. Der Datenbestand aktualisiert sich täglich automatisch per Delta-Import,
   inkl. Selbst-Wecken und Wieder-Einschlafen.
4. Läuft auf jedem Docker-Host; Referenz-Deployment ist Unraid
   (DB/Index auf dem Array, Wächter auf dem Cache-Pool).

## 2. Constraints

- **Host:** Unraid; Postgres + Index auf dem Array (Spindeln), Wächter +
  Cache-Daten auf dem SSD-Cache-Pool. Cache zu klein für den Datenbestand
  (dreistellige GB-Größe erwartet und akzeptiert).
- **API-Kompatibilität** zu api.acoustid.org ist Pflicht (Drittclients).
- **MusicBrainz:** Lokaler Spiegel vorhanden (musicbrainz-docker-Stack,
  eigene Postgres). Direkter Read-only-DB-Zugriff wird genutzt.
- **Datenquelle:** Öffentliche AcoustID-Datenbank; tägliche inkrementelle
  JSON-Update-Dateien (data.acoustid.org). Lizenz CC BY-SA 3.0;
  MusicBrainz-AcoustID-Mapping Public Domain.
- **Netz:** Primär LAN/VPN. Bei Exponierung nach außen zwingend
  `apikey`-Modus + Reverse-Proxy mit TLS (Doku-Hinweis).
- **Repo:** Öffentlich auf GitHub; GitHub Actions bauen alle Images nach
  GHCR; gemeinsamer Release-Tag für alle Images pro Release.
- **Kein Fingerprint-Berechnen serverseitig** (Chromaprint läuft im Client).

## 3. Architektur-Überblick

Ein Repo, fünf Container, zwei Compose-Dateien.

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
| `acoustid-index` | `ghcr.io/acoustid/acoustid-index` (Zig-`main`, per Digest gepinnt) | Fingerprint-Suchindex (Matching-Kern). Keine Auth — Port nie veröffentlichen, nur Compose-intern. Daten auf dem SSD-Cache-Pool (Entscheid 2026-07-25), NICHT auf dem Array |

**Datenflüsse:**
- Client → Wächter (Proxy) → API → Index (Match) + eigene Postgres
  (Mappings) + MB-Postgres (Metadaten, read-only).
- Scheduler (Wächter) → weckt Stack → startet Importer-Job → Deltas
  einspielen → Stack schläft wieder.
- Admin-UI läuft vollständig im Wächter; Aktionen, die den Stack brauchen,
  zeigen den Schlafzustand und bieten einen Weck-Button.

**Grundsatzentscheidungen:** siehe DECISIONS.md (Einträge 2026-07-25).

## 4. Technologie-Stack

Immer neueste stabile Version zum Implementierungszeitpunkt:

- **Sprache:** Python (API-Layer, Importer, Wächter — eine Sprache für alles)
- **Web-Framework:** FastAPI (API + Admin-UI-Routen)
- **UI:** Server-rendered — Jinja2-Templates + HTMX, kein Frontend-Build,
  kein SPA-Framework, kein npm
- **Datenbanken:** PostgreSQL (AcoustID-Daten), SQLite (Wächter-Zustand)
- **Suchindex:** acoustid-index (offizielles Image)
- **Deployment:** Docker Compose (zwei Dateien), Images via GHCR
- **CI:** GitHub Actions (Build, Tests, Multi-Image-Release mit einem Tag)

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

**Weitere Tabellen (Spalten werden in ihren Phasen festgelegt):**
| Tabelle | Inhalt |
|---|---|
| `local_submission` | Eigene Einreichungen: Fingerprint-Daten, Metadaten aus dem Submit, Zeitstempel, Status `new` → `indexed` → `forwarded` \| `forward_failed` (Phase 11/12) |

### 5.3 Matching-Pipeline (verifiziert in Phase 1)

Details: [docs/research/phase1-acoustid-index.md](docs/research/phase1-acoustid-index.md).

- **Zweistufig:** (1) Kandidaten aus dem acoustid-index
  (`POST /:index/_search`, limit 20–40, timeout 2000 ms, msgpack;
  Score dort = Integer-Trefferzahl, dient nur der Vorsortierung);
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
  dedupliziert, max. `index.query_hashes` Hashes (Default 120),
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
  `AOFF_INDEX_NAME` (Default `main`). Compose-Healthcheck prüft
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
  kanonische MBID; Config-Flag zum Durchreichen der eingereichten).
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
| `update_run` | Historie der Import-/Backup-Läufe: Start, Ende, eingespielte Dateien, Zeilen, Ergebnis, Fehlermeldung |
| `event_log` | Ereignisse (Start/Stopp, Wecken, Fehler, Notifications) mit Level und Zeitstempel, ringpuffer-artig begrenzt |
| Lookup-Cache | Eigene Tabelle oder Dateicache; Schlüssel = Hash(Fingerprint+Duration+meta-Parameter), Wert = serialisierte Antwort; invalidiert nach Delta-Import und nach lokaler Submission |

### config.yaml (Wächter, Cache)
Alle Laufzeit-Einstellungen (siehe §6). Vom Wächter gelesen/geschrieben;
der API-Layer erhält die relevante Teilmenge beim Start bzw. per
Reload-Signal vom Wächter.

## 6. Konfiguration — Schlüssel, Defaults, feste Werte

Laufzeit-Einstellungen in `config.yaml` (Cache-Volume des Wächters,
editierbar über die Admin-UI). Env-Variablen (Prefix `AOFF_`) nur für
Bootstrap (Pfade, Ports, DB-Zugänge).

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `auth.mode` | `none` | `none` \| `apikey` |
| `submit.mode` | `local` | `off` \| `local` \| `local+upstream` |
| `submit.upstream_app_key` | leer | Application-Key für api.acoustid.org |
| `wake.hold_timeout_s` | `90` | Max. Haltezeit einer Anfrage beim Wecken |
| `idle.timeout_min` | `15` | Auto-Stopp nach Inaktivität |
| `update.time` | `04:00` | Täglicher Delta-Import (lokale Zeit) |
| `update.min_free_gb` | `50` | Mindest-Plattenreserve vor Import (gelesen als GiB — strengere Lesart; `0` schaltet den Guard ab; gemessen wird das Dump-Verzeichnis, Phase 8) |
| `cache.enabled` | `true` | Lookup-Cache an/aus |
| `cache.max_size_mb` | `512` | Obergrenze Lookup-Cache |
| `ratelimit.per_ip_per_min` | `120` | Anfragen pro IP pro Minute |
| `metrics.enabled` | `false` | Prometheus-Endpoint |
| `notify.ntfy.url` | leer | ntfy/Webhook-Ziel (leer = aus) |
| `notify.smtp.*` | leer | Host, Port, User, Pass, From, To (leer = aus) |
| `backup.dir` | leer | Backup-Ziel (leer = Backup aus) |
| `backup.time` | `04:45` | Backup nach dem Update-Lauf |
| `mb.dsn` | leer | Read-only-DSN der MusicBrainz-Postgres |

**Projekt-Ergänzungen zur Config (entschieden 2026-07-25):**

| Schlüssel | Default | Bedeutung |
|---|---|---|
| `index.query_hashes` | `120` | Query-Hashes je Fingerprint im Suchindex; RAM-abhängig pro Host einstellbar (z. B. 80 bei wenig RAM). Änderung erfordert Index-Neuaufbau; Empfehlungstabelle entsteht aus dem Probelauf (Phase 8) |
| `auth.allow_known_client_keys` | `false` | `apikey`-Modus: fest einkodierte Keys bekannter Drittclients (Picard `v8pQ6oyB`, beets `1vOwZtEn`) zulassen — bewusst default aus, da öffentlich bekannt |

**Feste Werte:**
- **Port:** Wächter lauscht auf einem Port (default `8080`) für API-Proxy
  und Admin-UI unter `/admin`; Port per Env änderbar.
- **Container-Namen:** `acoustid-watchdog`, `acoustid-api`,
  `acoustid-importer`, `acoustid-db`, `acoustid-index`.
- **Batch-Limit:** max. 100 Einträge pro `/v2/lookup/batch`-Request.
- **Upstream-Retries:** nach 7 Fehlversuchen Notification + manueller
  Retry über die Admin-UI.
- **Idle-Definition:** keine API-Anfrage im Timeout-Fenster UND kein
  laufender Import-/Backup-Job.
- **Admin-Login:** ein Benutzer; Passwort-Hash (argon2) in der SQLite;
  Erst-Passwort beim ersten Start generiert und geloggt.
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
  nach `submit.mode`; Antwortformat identisch zum Original.

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
- **`POST /v2/lookup/batch`** — JSON-Body mit Array von
  `{fingerprint, duration, meta}`; Antwort: Array in gleicher
  Reihenfolge. Obergrenze 100 Einträge pro Request. (Zusätzlich gilt
  das Original-Batchprotokoll mit max. 20, s. o.)
- **`GET /status`** — Wächter-Endpoint, weckt nie: Stack-Zustand
  (schlafend/startend/bereit/Fehler), Datenstand (letzte Delta-Sequenz),
  letzter Update-Lauf, Version.
- **`GET /metrics`** — Prometheus-Format, nur wenn `metrics.enabled`.
- **`/admin/...`** — Admin-UI (server-rendered), Passwort-geschützt.

### Umsetzungsstand

`GET/POST /v2/lookup` ohne `meta` steht seit Phase 9 (`api/app/`,
Parameter/Formate/Fehlercodes und bewusste Abweichungen vom Original:
[docs/api-lookup.md](docs/api-lookup.md)). `meta`/MB-Resolver folgt in
Phase 10, Submit in 11/12, Batch-Endpoint + `/v2/submission_status`
in 13.

### Durchsetzungsort Auth & Rate-Limit
API-Key-Prüfung (`apikey`-Modus) und IP-Rate-Limit setzt der **Wächter**
am Proxy durch — auch für Cache-Hits bei schlafendem Stack (Entscheid
2026-07-25, siehe DECISIONS.md). Der API-Service selbst prüft keine Keys.

### Fehlerverhalten
- Aufwecken: Anfrage wird gehalten; erst nach `wake.hold_timeout_s`
  ohne Bereitschaft → `503` mit `Retry-After`.
- Stack-Start-Fehler → `503` + Fehlertext + Notification.
- Ungültiger/fehlender Key im `apikey`-Modus → Fehlerantwort im
  AcoustID-Fehlerformat.
- Rate-Limit überschritten → `429` + `Retry-After`.

## 8. Verhaltensregeln & Invarianten

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
   Delta-Import und nach jeder lokalen Submission vollständig invalidiert.
7. **Degradierter Betrieb bei MB-Ausfall.** Ist die MB-Postgres nicht
   erreichbar, liefert Lookup AcoustID-UUIDs + MBIDs ohne Metadaten
   (kein Fehler); Ereignis wird geloggt.
8. **Plattenplatz-Guard.** Vor jedem Import: freier Platz ≥
   `update.min_free_gb`, sonst Abbruch + Notification.
9. **Upstream-Queue.** Fehlgeschlagene Upstream-Submits bleiben in
   `local_submission` (`forward_failed`) und werden beim nächsten
   Update-Lauf erneut versucht; nach 7 Fehlversuchen Notification und
   manueller Retry über die Admin-UI.
10. **Secrets nie im Repo.** Alle Zugänge über `.env`/`config.yaml`;
    `.env.example` dokumentiert alles.
11. **Ein Release = ein Tag = alle Images.** Wächter, API und Importer
    werden immer gemeinsam getaggt und veröffentlicht.

## 9. Admin-UI (Referenz: docs/DESIGN_HANDOFF.md)

- **Technischer Rahmen (fix):** FastAPI + Jinja2 + HTMX im
  Wächter-Container unter `/admin`; ein Admin-Benutzer, Passwort-Login,
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
acoustid-offline/
├── docker-compose.yml            # Stack: api, importer (Profil: job), db, index
├── docker-compose.watchdog.yml   # Wächter (immer an)
├── .env.example                  # Alle Bootstrap-Env-Variablen (AOFF_*), dokumentiert
├── README.md                     # Setup Unraid + generisch, Bootstrap-Anleitung, Lizenzhinweis Daten
├── unraid/                       # Unraid-Community-App-Template (XML)
├── watchdog/
│   ├── Dockerfile                # Wächter-Image (klein, Dauerläufer auf Cache)
│   └── app/                      # Module: Proxy, Weck-/Docker-Steuerung, Scheduler,
│       ├── ...                   #   Admin-Routen, Auth, Lookup-Cache, Notify, Metrics
│       ├── templates/            # Jinja2-Templates der Admin-UI
│       └── static/               # CSS/JS (HTMX) der Admin-UI, kein Build
├── api/
│   ├── Dockerfile                # API-Image
│   └── app/                      # /v2/lookup, /v2/submit, /v2/lookup/batch,
│                                 #   Index-Client-Nutzung, MB-Resolver
├── importer/
│   ├── Dockerfile                # Importer-Image (One-Shot-Job)
│   └── app/                      # Delta-Download, Parser, DB-Import,
│                                 #   Index-Feed, Backup-Job
├── shared/                       # Python-Paket: Config-Schema, Modelle, Logging
├── docs/
│   ├── HANDOFF.md                # Gesamtspezifikation (Quelle dieser Datei)
│   └── DESIGN_HANDOFF.md         # UI-Spezifikation für Claude Design
├── tests/                        # Unit- + Integrationstests (Compose-basiert)
└── .github/workflows/            # ci.yml (Tests), release.yml (3 Images → GHCR, ein Tag)
```

Der Dateischnitt unterhalb der `app/`-Verzeichnisse ist im Handoff nicht
festgelegt und wird in den jeweiligen Phasen konkretisiert (dann hier
nachtragen).

**Paketierung (Phase 2):** uv-Workspace, Python 3.14, ruff + pytest.
Verzeichnisse wie oben; installierte Import-Namen weichen ab:
`acoustid_api`, `acoustid_importer`, `acoustid_watchdog`, `shared`
(Details/Begründung in DECISIONS 2026-07-25). Echte Dump-Fixtures sind
nicht committet — `tests/fixtures/fetch_fixtures.py` beschafft sie
reproduzierbar. Repo: https://github.com/shares92/acoustid-offline

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
   zweistufiges Rescoring, Digest-Pin, `index.query_hashes`).
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
    `index.query_hashes`-Wert: aus dem Probelauf (Phase 8) ableiten.
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
15. Config-Flag für MBID-Redirect-Durchreichung (§5.4) hat noch keinen
    §6-Schlüssel — wird bei Bedarf in Phase 10 als Projekt-Ergänzung
    definiert (dann §6 + DECISIONS + Schema nachziehen).

Risiken (Priorität aus dem Handoff):
1. **Dump-Format/Bootstrap (hoch)** — größtes Projektrisiko, zuerst
   verifizieren, bevor weitere Phasen starten.
2. acoustid-index-Unbekannte (mittel).
3. Postgres auf Spindeln (mittel) — mitigiert durch Stale-Serving +
   nächtliches Zeitfenster.
4. MB-Schema-Kopplung (niedrig) — mitigiert durch Query-Schicht +
   degradierten Betrieb.
5. docker.sock im Wächter (akzeptiert) — minimaler Code, Passwort-Login,
   Rate-Limit, LAN-Betrieb.
6. Lizenz CC BY-SA 3.0 (niedrig) — README-Hinweis bei Weitergabe.
