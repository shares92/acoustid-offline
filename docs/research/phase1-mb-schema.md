# Phase-1-Recherche: MB-Metadaten-Auflösung & Query-Schicht (2026-07-25)

Recherche-Report der Implementierungs-Session (Opus-Agent, von Fable
synthetisiert). Quellen: `acoustid-server` @ `acae927` (v26.3.1),
`musicbrainz-server` @ `11cd769`, `musicbrainz-docker` @ `3b835fd`,
`mbdata` @ `a3ff890`.

## Referenz acoustid-server

Das gesamte MB-Wissen steckt in `acoustid/data/musicbrainz.py`
(315 Zeilen); Tabellen aus `mbdata==25.0.0`, Schema `musicbrainz`,
eigene Read-only-Engine. `lookup_metadata(recording_ids,
load_releases, load_release_groups, load_artists)` feuert bis zu 6
Queries:

- **Q-A Hauptquery:** `recording` (Filter `gid IN`), bei releases
  zusätzlich INNER JOINs `track`→`medium`→`release` + LEFT
  `medium_format`.
- **Q-B Artists (immer):** `artist_credit_name JOIN artist` über
  AC-IDs, `ORDER BY artist_credit, position`.
- **Q-C Release-Meta:** `medium` GROUP BY release (Anzahl Medien,
  Summe track_count).
- **Q-D Release-Events:** View `release_event` LEFT JOIN `iso_3166_1`.
- **Q-E/Q-F Release-Groups:** `release_group` + primary/secondary
  types.
- Nebenqueries: `replication_control.last_replication_date`;
  `resolve_mbid_redirect` existiert, ist aber **toter Code** (keine
  Aufrufstelle).

meta-Parameter → Query-Bedarf: `recordingids` bräuchte gar keine
MB-Query (Referenz macht sie trotzdem — Verschwendung, weglassbar);
`sources` und `usermeta` brauchen **keine** MB-Query (eigene DB);
`tracks`/`compress` sind Strukturmodifikatoren auf Q-A-Daten.

Fehlende MBIDs: kein Fehler — der Server loggt und enqueued
`merge_missing_mbid` (async); nach 7 Tagen (gemessen an
`last_replication_date`) wird die MBID via `disable_mbid` deaktiviert.

## Benötigte MB-Tabellen (17)

`recording` (id, gid UNIQUE, name, length[ms, NULL], artist_credit,
comment, video) · `artist_credit` (id, name) · `artist_credit_name`
(PK artist_credit+position; name, join_phrase, artist) · `artist`
(id, gid, name, sort_name) · `track` (recording→, medium→, gid,
position, number, name, artist_credit, length, is_data_track; Index
`track_idx_recording`) · `medium` (release→, position, format NULL,
name, track_count) · `medium_format` (name) · `release` (id, gid,
name, artist_credit, release_group→) · `release_group` (id, gid,
name, artist_credit, type NULL) · `release_group_primary_type` ·
`release_group_secondary_type_join` · `release_group_secondary_type` ·
`release_event` (**VIEW** = UNION aus `release_country` +
`release_unknown_country`) · `iso_3166_1` (area→, code CHAR(2)) ·
`recording_gid_redirect` (gid PK, new_id→recording.id) ·
`replication_control` (id=1: current_schema_sequence,
current_replication_sequence, last_replication_date).

### Fallstricke (bewusste Abweichungen von der Referenz)

1. INNER JOINs der Referenz lassen Release-lose Recordings komplett
   verschwinden → wir machen getrennte Queries (Basis immer separat).
2. `recording.length / 1000` ist Integer-Division → **abschneiden,
   nicht runden** (Kompatibilität).
3. Referenz indiziert Dicts direkt (KeyError-anfällig) → wir `.get()`.
4. `medium.track_count` enthält Data-Tracks; nicht filtern
   (Kompatibilität), Spalte mitziehen.
5. `release_event.country` kann NULL sein → LEFT JOIN auf iso_3166_1
   ist Pflicht.
6. `artist_credit` ist dedupliziert/geteilt → immer Batch über
   AC-ID-Menge, nie pro Recording joinen.

## Redirects & Merges

16 `*_gid_redirect`-Tabellen in MB; für uns nur
`recording_gid_redirect` relevant (Eingabe-MBIDs aus `track_mbid`
altern; Release-/RG-/Track-MBIDs lesen wir immer frisch aus den
kanonischen Tabellen). **Entschieden (2026-07-25): Online-Auflösung
bei Misses** — nicht gefundene MBIDs in einem zweiten Batch gegen
`recording_gid_redirect` auflösen und erneut abfragen; Antwort enthält
die **kanonische** MBID (Config-Flag für Durchreichung der
eingereichten). Optional später: periodischer `track_mbid`-Rewrite
(Optimierung). Ohne Redirect-Auflösung lieferte die Instanz für
gemergte Recordings dauerhaft leere Metadaten — der realistische
Haupt-Fehlerfall.

## musicbrainz-docker-Spezifika

- DB `musicbrainz_db`, User/Pass Default `musicbrainz`/`musicbrainz`,
  Port 5432 nur `expose`; Host-Port via
  `admin/configure add publishing-db-port` (dabei
  `MUSICBRAINZ_DOCKER_HOST_IPADDRCOL=127.0.0.1:` setzen — sonst bindet
  es auf alle Interfaces). Alternative: unser Stack ins Compose-Netz
  des Spiegels hängen (Host `db`).
- Schema `musicbrainz`; Postgres 18.
- **Kein Read-only-Setup mitgeliefert.** Rolle manuell anlegen
  (dokumentiertes SQL-Snippet, Betreiber führt es einmalig aus):
  ```sql
  CREATE ROLE acoustid_ro LOGIN;                 -- Passwort separat
  GRANT CONNECT ON DATABASE musicbrainz_db TO acoustid_ro;
  GRANT USAGE  ON SCHEMA musicbrainz TO acoustid_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA musicbrainz TO acoustid_ro;
  ALTER ROLE acoustid_ro SET search_path TO musicbrainz, public;
  ALTER ROLE acoustid_ro SET default_transaction_read_only = on;
  ALTER ROLE acoustid_ro SET statement_timeout = '2s';
  ALTER DEFAULT PRIVILEGES IN SCHEMA musicbrainz
        GRANT SELECT ON TABLES TO acoustid_ro;
  ```
  Runbook: nach Schema-Upgrades GRANT SELECT erneuern.
- **Schema-Versionierung:** `replication_control.current_schema_sequence`
  (aktuell **31**; Änderung ~1×/Jahr Mitte Mai; 32 bereits im
  master-Tree für 2027). Risiko **niedrig**: Schema-Changes 26→32
  enthielten für unsere 17 Tabellen nur zwei rein additive ALTERs
  (artist_credit.gid, medium.gid). Mit expliziten Spaltenlisten +
  Schema-Qualifizierung sind additive Changes No-Ops.
- Replikation: täglicher Cron (03:00) → Spiegel hinkt bis 24 h nach.
  Staleness-Schwellen: WARN > 36 h, CRIT > 7 d.

## Query-Schicht (entschieden: Raw-SQL, kein mbdata)

Designprinzipien: alles schema-qualifiziert; explizite Spaltenlisten;
Batch über `= ANY(:arr::typ[])`; getrennte Funktionen pro Ebene (keine
Monster-Query); keine Fachlogik; **nur eine Datei kennt
MB-Tabellennamen** (CI-Grep-Regel: `musicbrainz\.` nur in
`mb/queries.py`); alle Funktionen Batch-first mit Dict-Rückgabe.

Fehlerbild (eigene Exceptions, SQLAlchemy dringt nie nach außen):
`MbUnavailable` (Connect/Pool/Timeout → degradiert antworten, HTTP
200), `MbSchemaMismatch` (Selfcheck → laut loggen, degradiert
starten), `MbStale` (nur Metrik/Log), `MbQueryError` (500 + Alert,
nicht degradieren). DSN-Optionen: `search_path`,
`statement_timeout=2000`, `default_transaction_read_only=on`,
`idle_in_transaction_session_timeout=5000`, `connect_timeout=2`.
Eigener kleiner Pool (`pool_pre_ping`, `pool_timeout` 1–2 s;
Referenz: 20/2). **Circuit Breaker** davor (nach N Fehlern in T
Sekunden für X Sekunden sofort MbUnavailable). Alle Aufrufe eines
Requests in einer Read-only-Transaktion.

Funktionen (SQL-Skelette im Detail siehe Recherche-Transkript;
Kernpunkte):

| # | Funktion | Zweck | meta-Varianten |
|---|---|---|---|
| 0 | `mb_health()` | replication_control-Zeile (Schema-Guard + Staleness) | Startup/periodisch |
| 0b | `mb_selfcheck()` | pg_attribute-Diff gegen Erwartungsliste | Startup |
| 1 | `resolve_recording_redirects(mbids)` | recording_gid_redirect JOIN recording | alle (nur Misses) |
| 2 | `existing_recording_mbids(mbids)` | Index-Only-Existenzprüfung | recordingids |
| 3 | `recordings_by_mbids(mbids)` | recording JOIN artist_credit (Titel, Länge ms, AC-ID, AC-String) | R/REL/RG/T/M2 |
| 4 | `artist_credits(ac_ids)` | acn JOIN artist, ORDER position — ein Call für ALLE AC-IDs | R/REL/RG/T/M2 |
| 5 | `recording_release_rows(mbids)` | track→medium→release→release_group (+format); **limit_rows-Kappung (~5000) + Truncation-Flag — DoS-Vektor!** | REL/RG/T/M2 |
| 6 | `release_counts(release_ids)` | medium GROUP BY release | REL/RG/M2 |
| 7 | `release_events(release_ids)` | release_event-View (+ Fallback release_country UNION release_unknown_country, per Flag) | REL/RG/M2 |
| 8 | `release_groups(rg_ids)` | rg + primary_type | RG |
| 9 | `release_group_secondary_types(rg_ids)` | join + types, ORDER child_order (deterministischer als Referenz) | RG |

Choreografie: `recordings` = 3 → 1(Misses) → 3(retry) → 4 (2–4
Roundtrips); `releases(+tracks)` = 3,5,6,7,4; `releasegroups` =
3,5,6,7,8,9,4; `releaseids`/`releasegroupids` = nur 5 (RG-MBID steckt
in 5). 6/7 bzw. 8/9 parallelisierbar.

## Offene Punkte

1. Zeilenlimit Funktion 5 (~5000) am echten Spiegel messen.
2. `statement_timeout` 2 s am echten Spiegel verifizieren
   (EXPLAIN ANALYZE Funktion 5).
3. Caching (replication_sequence als Generationszähler) — als
   Erweiterungspunkt vorgesehen, nicht bauen.
4. Ob View `release_event` + Mirror-Indizes im konkreten Spiegel
   existieren (Standard-createdb: ja) — deckt der Selfcheck ab.
