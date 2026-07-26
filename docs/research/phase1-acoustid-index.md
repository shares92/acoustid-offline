# Phase-1-Recherche: acoustid-index (2026-07-25)

Recherche-Report der Implementierungs-Session (Opus-Agent, von Fable
synthetisiert). Belegbasis: Clones von `github.com/acoustid/acoustid-index`
(Branches `main`, `ng`) und `github.com/acoustid/acoustid-server`,
GHCR-/Docker-Hub-Registry-APIs, eigenes Größenmodell (Encoder aus
`src/block.zig`/`src/streamvbyte.zig` nachgebildet).

## Version & Image

Zwei aktive Codebasen:

| | `main` (v1, Zig) | `ng` (v2, Zig) |
|---|---|---|
| Letzter Commit | 2025-10-27 (`6bc929a`) | 2026-07-21 (`180aab0`) |
| Zig-Version | 0.14 | 0.16 |
| Speicher | mmap + Page-Cache | voll RAM-resident, mlock, kein mmap |
| Cluster | NATS JetStream | PostgreSQL-Changelog |
| Docker-Image | ja, `ghcr.io/acoustid/acoustid-index:main` | nein |

- Einziger Tag der Zig-Ära: `v25.4.0` (2025-04-09); kein GitHub-Release
  dazu. GPL-3.0, faktisch Ein-Personen-Projekt (Lukáš Lalinský).
- `ng`-README: „single-node feature-complete (parity … bar the cluster)";
  Design-Notes 2026-07: „No backwards compatibility, period" — Migration
  = Index-Neuaufbau. **Aber: `ng/src/api.zig` ist wire-kompatibel**
  (gleiche Endpoints/Strukturen; nur zwei optionale neue Suchparameter
  `min_score`/`m`, `score_pct`/`s`).
- Images: Docker Hub existiert nicht; quay.io nur C++-Ära (≤2021);
  **GHCR ist der relevante Ort**: `main` (beweglich, 2025-10-27),
  `latest` = v25.4.0, `stable` = C++ 2022. Ausschließlich linux/amd64.
- Image (`Dockerfile`): ubuntu:24.04, ~74 MB, ReleaseFast-Binary aus CI,
  **USER acoustid UID 6081**, VOLUME `/var/lib/acoustid-index`,
  EXPOSE 6081, CMD `fpindex --dir /var/lib/acoustid-index --address
  0.0.0.0 --port 6081`.
- **Empfehlung (übernommen): `ghcr.io/acoustid/acoustid-index:main` per
  Digest pinnen** — kein Release-Prozess, `main` ist beweglich.
- Kein öffentlicher Beleg, dass acoustid.org die Zig-Version produktiv
  fährt (deren compose pinnt noch `v2022.02.03` C++, Port 6080,
  Legacy-Zeilenprotokoll; `legacy-proxy/` existiert als Brücke).

## API

Routen aus `src/server.zig:125-150`. **README ist mehrfach veraltet** —
Formate unten aus `src/api.zig` + `tests/`.

### Index anlegen — `PUT /:index`

Request `{}` (optional `{"expect_does_not_exist": true, "generation": n}`)
→ `{"version": 0, "ready": true, "generation": 1}`; 200, oder 202 wenn
`ready=false`; Konflikte 409 (`IndexAlreadyExists`,
`OlderIndexAlreadyExists`, `NewerIndexAlreadyExists`, `IndexBeingDeleted`).

### Dokumente — `POST /:index/_update`

```json
{"changes": [
   {"insert": {"id": 12345, "hashes": [4294967280, 1234567888]}},
   {"delete": {"id": 11111}}
 ],
 "metadata": {"last_fp_id": "12345"},
 "expected_version": 41}
```
→ `{"version": 42}`.

- **Atomar pro Request**: ein MemorySegment aus allen Changes, eine
  msgpack-Transaction ins Oplog, fsync, eine Versionsnummer.
- Doppelte IDs im Batch: der **letzte** Change gewinnt.
- `expected_version` = optimistisches Locking → 409 `VersionMismatch`
  (nutzbar für idempotenten Bulk-Load).
- **Empfohlene Batchgröße 1000** (benchmark.py und offizieller
  Consumer `BATCH_SIZE = 1000`).
- Harte Grenze `max_body_size = 16 MiB` (~26k Changes msgpack / ~11,6k
  JSON bei 120 Hashes).

### Suche — `POST /:index/_search`

`{"query": [u32…], "timeout": 500, "limit": 40}` →
`{"results": [{"id": 12345, "score": 118}]}`

- **`score` (u32) = Anzahl der Query-Hashes, die im Dokument vorkommen**
  (0…query_len). Kein Float, keine Normierung — nur Vorsortierung.
- Parameter: `timeout` Default 500 ms, max 10000; `limit` Default 40,
  1…100.
- Schwellen fest verdrahtet: absolut `min_score = (query_len+19)/20`
  (bei 120 → 6); relativ Cutoff bei 10 % des besten Treffers.
- Query-Array wird in-place sortiert; **Duplikate zählen doppelt** —
  Extraktion muss deduplizieren (tut `acoustid_extract_query`).
- **Timeout → HTTP 500** `{"error":"Timeout"}`, kein Teilergebnis.
- Interne Kappungen: `MAX_BLOCKS_PER_HASH = 4`, `MAX_DOCS_PER_HASH =
  1000` (sehr häufige Hashes werden abgeschnitten — wie im Original).

### Delete / Health / Metrics / Auth

- `DELETE /:index/:fpid` (Tombstone); kein Truncate → `DELETE /:index`
  + `PUT /:index`.
- `GET /_health` prüft **nichts** (Liveness); `GET /:index/_health` nur
  Existenz; echter Readiness-Indikator: 503 `IndexNotReady`.
- `GET /_metrics` Prometheus: `aindex_docs`, `aindex_search_duration_seconds`,
  `aindex_scanned_blocks_per_hash`, `aindex_checkpoints_total`,
  `aindex_file_segment_merges_total`.
- **Keine Auth, kein TLS → Port niemals veröffentlichen**, nur
  Compose-intern.

### Content-Type-Falle & msgpack

Body ohne `Content-Type`-Header ⇒ Server nimmt **msgpack** an, nicht
JSON. Für Produktion msgpack nutzen (Feldnamen = erster Buchstabe):
`search {"q","t","l"} → {"r":[{"i","s"}]}`,
`update {"c":[{"i":{"i","h"}},{"d":{"i"}}]} → {"v"}`,
Header `Content-Type`/`Accept: application/vnd.msgpack`.

## Persistenz & Crash

LSM-artig: Oplog (WAL) → MemorySegments → Checkpoint → FileSegment →
Tiered Merge.

1. **Oplog: fsync bei jedem `_update`** — quittiert = durable. Dateien
   `oplog/<commit_id:016x>.xlog`, Rotation bei 16 MiB.
2. Segment-Dateien atomar (tmp + fsync + rename), CRC64-Footer.
3. Manifest atomar + Hardlink-Backup `manifest.backup` — **das Backup
   wird im Ladepfad aber nie gelesen** (Korruption ⇒ manueller Eingriff).
4. Recovery: Manifest → Segmente laden → Oplog-Replay ab
   `last_commit_id+1`; Crash zwischen Checkpoint-Schritten ⇒ harmloses
   Re-Replay.
- Backup online: `GET /:index/_snapshot` (tar; Manifest + FileSegments,
  **ohne Oplog/MemorySegments** — konsistent, nicht zwingend aktuell).
  Dateikopie nur im Stillstand (Merges löschen Dateien). Kein
  Restore-Endpoint (PR #143 offen) — Restore = tar entpacken, Layout
  `<dir>/<index>/<name>-<generation>/{manifest, oplog/, *.data}`.
- Rebuild-Kosten (Schätzung, Konstanten aus `src/Index.zig`): Checkpoint
  je 500k Items, `max_segment_size` 750M, 10 Segmente/Level ⇒ ~24.000
  Checkpoints, ~4 Merge-Ebenen, Write-Amplification ~5× ⇒ ~240 GB
  geschrieben, ~190 GB gelesen, ~49 End-Segmente, 100k Requests à 1
  fsync. Realistisch Stunden bis ~1 Tag auf SSD.
- Notnagel `segment_builder`-Tool (stdin `"<hash> <docid>"` →
  Segmentdatei direkt, vorsortiert nötig, RAM-gebunden, Manifest
  manuell) — undokumentiert.

## Kennzahlen

Design-Ziele des Autors (CLAUDE.md/design-notes, keine Messungen):
100M Fingerprints à ~150 Hashes, Suche <50 ms, ≤64 GB RAM/Node,
„**Hard invariant: the index always fits in RAM**", ~40 GB Daten.

Eigenes Modell (Encoder nachgebaut, Gleichverteilungsannahme = obere
Schranke): 3,9–4,5 B/Item ⇒ **46,8–54 GB** für 12e9 Items (92–104 Mio.
FP à ≤120 Hashes) + ~0,4 GB Block-Index (mmap) + ~0,6 GB docs-Map.
**Planungswert Platte 40–55 GB, Volume ~70 GB** (Merge-Transient +
Oplog + Reserve). Hebel: **Einspielen in aufsteigender fp_id ⇒ ~15 %
kleiner**; Query-Hash-Anzahl 80 statt 120 ⇒ ~2/3 der Größe.

RAM: Page-Cache = Indexgröße (40–55 GB) + docs-HashMap **im Heap**
(~0,8–1,5 GB bei 100 Mio. Docs) + MemorySegments (≤64 MB) + Threadpool.

**mmap-Verhalten (`src/filefmt.zig`):** `MAP_POPULATE` + `MADV_WILLNEED`
⇒ beim Start wird die komplette Indexdatei gelesen (NVMe ~15–30 s,
SATA-SSD ~1,5–2 min, HDD ~6–10 min — bei jedem Start). `MADV_RANDOM`
schaltet Readahead ab. Kalte Suche (Rechnung): ~5.000–10.000 zufällige
Seitenzugriffe/Query, seriell ⇒ HDD ~40–80 s (= Timeout/500), SATA-SSD
~0,5–1 s, NVMe ~0,4–0,8 s, warm wenige ms. **Der Index muss praktisch
vollständig im Page-Cache liegen.**

## Konfiguration (vollständig, `src/main.zig`)

`--dir` (Default /tmp/fpindex; Image: /var/lib/acoustid-index),
`--address` (Image: 0.0.0.0), `--port` (6081), `--threads` (CPU-Anzahl;
httpz-Pool + Scheduler), `--log-level`, `--parallel-loading-threshold`
(8), `--cluster`/`--nats-url` (irrelevant). **Nicht konfigurierbar:**
Speicherlimit, Cache, Segment-/Blockgrößen, Oplog-Retention, Auth, TLS.

Compose-Hinweise: `user: "6081:6081"` (Unraid-Share entsprechend
chownen, nicht 99:100); kein `ports:`; Healthcheck auf
`/:index/_health` mit **`start_period` ~15 min** (Start liest ~50 GB);
Share „Prefer/Only: Cache" (sonst schiebt der Mover den Index aufs
Array).

## Entscheidungen (vom Betreiber 2026-07-25 bestätigt)

- **Platzierung: SSD-Cache-Pool** (~70-GB-Volume). Array scheidet aus
  (Startzeit, Timeout bei Cache-Verdrängung).
- **Rescoring: zweistufig.** Index liefert nur Kandidaten (limit 20–40,
  timeout 2000); AcoustID-Score via `acoustid_compare2`-Nachbau in
  Python gegen den Vollvektor aus Postgres, Längenfilter ±7, Cutoff
  >0,4 (Lookup) / >0,75 (Merge). CI verifiziert bit-genau gegen die
  Original-C-Extension (nur Test-Container).
- **Query-Hash-Anzahl konfigurierbar** (Default 120; RAM-abhängig 80) —
  Änderung erfordert Index-Neuaufbau.

## Warnungen aus dem acoustid-server-Code

1. **Legacy-Suchpfad liefert im Lookup null Treffer**: Index-Aufruf
   steckt in `if not self.fast:`, Lookup nutzt `fast=True` ⇒ ohne
   fpstore-Konfiguration findet der offizielle Server selbst gehostet
   nichts (`SEARCH_ONLY_IN_DATABASE` ist hartcodiert False).
2. **Offizielle Python-Referenz von `extract_query` ist defekt**
   (`array('i')` + unsigned Werte ⇒ OverflowError bei ~50 % realer
   Hashes). Autoritativ ist die C-Funktion der Extension; unser
   Python-Nachbau muss gegen sie verifiziert werden.
3. Bestätigt: der Index bekommt nur extrahierte Query-Hashes
   (size=120, start=80, 28-Bit-Maske 0xFFFFFFF0, SILENCE_HASH
   627964279, dedupliziert, unsigned).
4. Schwellwerte (`acoustid/const.py`): `TRACK_GROUP_MERGE_THRESHOLD
   0.4` (Lookup-Cutoff), `TRACK_MERGE_THRESHOLD 0.75`,
   `FINGERPRINT_MERGE_THRESHOLD 0.9`, `TRACK_MAX_OFFSET 80`,
   `FINGERPRINT_MAX_LENGTH_DIFF 7`, max. 10 Ergebnisse je Query;
   Kandidaten 10 (fast) / 20 (gründlich).

## Addendum: Empirische Befunde aus Phase 5 (2026-07-25)

Beim Bau des Index-Clients gegen das echte Image (Digest `c27a9926…`,
main @ 2025-10-27) verifiziert:

1. **Kurzfeldnamen gelten auch für Requests** (`{"c": …}`, PUT: `e`/`g`);
   der Server akzeptiert beide Formen, wir senden kurz.
2. **`GET /:index` existiert**: `{v, m, s}` (Version, Metadaten,
   Statistik) — Statistik-Felder in Langform (`min_doc_id`,
   `max_doc_id`, `num_segments`, `num_docs`).
3. **Fehlerrümpfe:** `{"e": "<Kennung>"}` — u. a. `InvalidFormat`,
   `MissingStructFields`, `IndexNotFound`, `VersionMismatch`,
   `InvalidIndexName`, `InvalidCharacter`, `Timeout`.
4. **`limit` wird still auf 100 gedeckelt** (0 → 1); `timeout`
   außerhalb 1…10000 wird kommentarlos angenommen → Client validiert
   selbst vor dem Senden.
5. **Such-Timeout → HTTP 500 `{"e":"Timeout"}`** real reproduziert,
   kein Teilergebnis (bestätigt).
6. `GET /:index/_health` bei unbekanntem Index: nackte 404 ohne Rumpf.
7. **Content-Type-Falle bestätigt**; `Accept` steuert das
   Antwortformat, ohne `Accept` antwortet der Server msgpack.
8. **Indexnamen-Regeln:** `[A-Za-z0-9][A-Za-z0-9_-]*`; Verstöße →
   `InvalidIndexName`/`InvalidCharacter`.
9. **Tombstones zählen in `num_docs` mit** — keine Zahl lebender
   Dokumente (relevant für Index↔Postgres-Abgleiche).
10. **Metadaten-Update ohne Changes erlaubt** und erhöht die Version —
    als Fortschrittsmarke für den Import nutzbar.
11. `_snapshot` liefert `application/x-tar` (Backup-Weg bestätigt).
12. Bestätigt: doppelte ID im Batch → letzter gewinnt; Version startet
    bei 0, +1 je `_update`; `DELETE /:index` → `{"d": true}`.
13. **Image ist amd64-only** und hängt auf Apple Silicon unter
    qemu-Emulation still (Prozess ohne Listen-Socket); mit
    colima `--vz-rosetta` läuft es. Healthcheck-Werkzeuglage im Image:
    nur `wget` (kein curl/nc/python3); `fpindex` hat keinen
    Check-Modus (unbekanntes Argument → Illegal instruction).
14. **Dokument-IDs sind u32** (Befund aus Phase 11, gleiches Image):
    `1`, `2^31-1`, `2^31` und `2^32-1` werden angenommen und
    unverändert wiedergefunden; `2^32` und alles darüber quittiert der
    Server mit HTTP 400 `IntegerOverflow` für den **ganzen** Batch —
    lauter Fehler, kein stilles Wrappen. Weder README noch `api.zig`
    dokumentieren das; der Client validiert seit Phase 11 gegen u32
    (`shared/shared/fpindex/wire.py`), und der Bereich `[2^31, 2^32-1]`
    ist für lokale Submissions reserviert (ARCHITECTURE §5.2/§5.3).

## Offene Punkte

1. `ng`-Umstieg: beobachten; wire-kompatibel, aber Index-Neuaufbau
   nötig; kein Release-Datum.
2. Reale Indexgröße: mit ~1 Mio. echten Fingerprints messen (Probelauf
   Phase 8), bevor das Volume final dimensioniert wird.
3. `min_score`/`score_pct` auf `main` nicht steuerbar (fest verdrahtet).
4. fpstore-Scoring nicht öffentlich — exakte Score-Parität mit
   acoustid.org nur empirisch prüfbar (optional).
5. Restore-Prozedur (`_snapshot` → tar entpacken) einmal manuell testen
   und dokumentieren.
6. `manifest.backup` wird nie gelesen — im Backup-Konzept einplanen.
