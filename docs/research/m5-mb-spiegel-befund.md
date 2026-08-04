# M5/M7-Gate: Empirischer Befund am MusicBrainz-Spiegel (2026-08-04)

Prüfauftrag zu Risiko **R19** / HANDOFF v2 §2, §8 „Externe Referenzen", §14.5.
Frage: Liefert der lokale `musicbrainz-docker`-Spiegel des Betreibers neben den
Metadaten auch **(b)** das `cover_art_archive`-Schema als Crawler-Verzeichnis und
**(c)** die MBID↔Discogs-Zuordnung über URL-Relationships — und ist beides für die
Read-only-Rolle des Projekts zugreifbar?

Durchführung: **strikt read-only** über `ssh Tower` (Betreiber-Freigabe lag vor).
Ausschließlich `docker ps/inspect/port`, `psql` mit `SELECT`/`\d`/`\dn`/`\dt`/`\du`
sowie `EXPLAIN (COSTS OFF)`. Nichts gestartet, gestoppt, geschrieben oder geändert.

---

## Ergebnis in einem Satz

**(b) und (c) sind vollständig vorhanden und aktuell repliziert** — das Gate ist
inhaltlich offen. Blockierend sind aber **drei Betriebs-Punkte**, die noch keiner
angefasst hat: die Rolle `acoustid_ro` **existiert nicht**, es gibt **keinen
Netzwerkpfad** vom Projekt-Stack zur MB-Datenbank, und die **`filesize`-Spalten des
CAA-Schemas sind zu 100 % NULL** (die Replikation liefert sie nicht mit).

| Prüfpunkt | Befund |
|---|---|
| MB-Stack läuft | ja, 5 Container, seit ~3 h |
| Schema `musicbrainz` + 17 Relationen | ✅ vollständig |
| Schema `cover_art_archive` | ✅ vorhanden, 7,38 Mio. Bilder, 3,76 Mio. Releases mit Front |
| URL-Relationships Discogs | ✅ vorhanden, Link-Types identifiziert |
| Replikation aktuell | ✅ stündlich, Stand 17 min alt |
| CAA wird mitrepliziert | ✅ ja (Beleg unten) |
| Rolle `acoustid_ro` | ❌ **existiert nicht**, keinerlei GRANTs gesetzt |
| Netzwerkpfad Projekt → MB-DB | ❌ **fehlt** (getrennte Docker-Netze, kein Host-Port) |
| `cover_art.filesize` nutzbar | ❌ **0 von 7.383.624 Zeilen befüllt** |

---

## 1. Bestandsaufnahme

```
$ ssh Tower 'docker ps -a --format "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"'
musicbrainz-docker-musicbrainz-1  musicbrainz-docker-musicbrainz   Up 3 hours  0.0.0.0:5000->5000/tcp
musicbrainz-docker-indexer-1      musicbrainz-docker-indexer       Up 3 hours
musicbrainz-docker-search-1       musicbrainz-docker_search:4.1.0  Up 3 hours  8983/tcp
musicbrainz-docker-db-1           musicbrainz-docker_db:18         Up 3 hours  5432/tcp
musicbrainz-docker-valkey-1       valkey/valkey:9-alpine           Up 3 hours  6379/tcp
```

Der Stack läuft — nichts musste gestartet werden. Relevanter Container:
**`musicbrainz-docker-db-1`**.

```
$ ... psql -U musicbrainz -d musicbrainz_db -c "SELECT version();"
PostgreSQL 18.3 (Debian 18.3-1.pgdg13+1) on x86_64-pc-linux-gnu, ... 64-bit
```

Postgres **18.3** — deckt sich mit der Annahme „Postgres 18" aus
`phase1-mb-schema.md`.

### Zwei Datenbanken — nur eine ist echt (Stolperfalle für `mb.dsn`)

```
$ ... psql -U musicbrainz -d postgres -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) ..."
    datname     | groesse
----------------+---------
 musicbrainz_db | 69 GB
 template1      | 7750 kB
 postgres       | 7678 kB
 template0      | 7678 kB
 musicbrainz    | 7521 kB

$ ... psql -U musicbrainz -d musicbrainz -c "\dn"
  Name  |       Owner
--------+-------------------
 public | pg_database_owner      <- nur public, keine Nutzdaten
```

Die Datenbank heißt **`musicbrainz_db`** (69 GB). Daneben liegt eine **leere
Datenbank `musicbrainz`** (7,5 MB, nur `public`). Ein DSN mit
`dbname=musicbrainz` verbindet sich erfolgreich — und findet dann **nichts**.
Genau diese Form steht aktuell in `shared/tests/test_config_schema.py:229`
(`"host=mb dbname=musicbrainz user=acoustid_ro"`) und in
`shared/tests/test_config_io.py:42`. Das ist in Tests unschädlich (dort wird nur
die Schema-Validierung geprüft, es wird nie verbunden), taugt aber **nicht als
Vorlage** für die echte Konfiguration. Hinweis an M5: im Beispiel-DSN und in der
Betreiber-Doku konsequent `dbname=musicbrainz_db` schreiben.

### Schemas

```
$ ... psql -U musicbrainz -d musicbrainz_db -c "\dn"
 cover_art_archive | musicbrainz      <- (b) VORHANDEN
 dbmirror2         | musicbrainz
 documentation     | musicbrainz
 event_art_archive | musicbrainz
 json_dump         | musicbrainz
 musicbrainz       | musicbrainz      <- (a) genutzt
 public            | pg_database_owner
 report | sir | sitemaps | statistics | wikidocs
```

---

## 2. Schema `musicbrainz` — alle benötigten Relationen vorhanden

Referenz ist `EXPECTED_COLUMNS` in `shared/shared/mb/queries.py` (17 Relationen)
plus die View `release_event` (in `mb_selfcheck` separat über `RELEASE_EVENT_VIEW`
ergänzt → 18 geprüfte Relationen).

```sql
WITH needed(name) AS (VALUES ('recording'),('artist_credit'),... )
SELECT n.name, CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view'
       ELSE COALESCE(c.relkind::text,'FEHLT') END AS kind, c.reltuples::bigint
FROM needed n LEFT JOIN pg_class c
  ON c.relname = n.name AND c.relnamespace = 'musicbrainz'::regnamespace;
```

| Relation | Art | est. Zeilen (`reltuples`) |
|---|---|---|
| `recording` | table | 39.565.820 |
| `track` | table | 57.061.064 |
| `artist_credit` | table | 3.810.542 |
| `artist_credit_name` | table | 7.077.521 |
| `artist` | table | 2.934.514 |
| `medium` | table | 6.217.298 |
| `medium_format` | table | 115 |
| `release` | table | 5.658.985 |
| `release_group` | table | 4.420.534 |
| `release_group_primary_type` | table | 5 |
| `release_group_secondary_type` | table | 12 |
| `release_group_secondary_type_join` | table | 961.703 |
| `release_country` | table | 13.224.426 |
| `release_unknown_country` | table | 464.306 |
| `release_event` | **view** | (View) |
| `iso_3166_1` | table | 258 |
| `recording_gid_redirect` | table | 5.002.294 |
| `replication_control` | table | 1 |

**Kein Eintrag „FEHLT".** Die View `release_event` existiert, der in
`phase1-mb-schema.md` Offener Punkt 4 vermutete Standardfall trifft zu — der
Fallback „`release_country` UNION `release_unknown_country`" wird nicht gebraucht
(beide Tabellen sind aber vorhanden, der Flag-Fallback bleibt also lauffähig).

### Replikationsstand

```
$ ... -c "SELECT * FROM musicbrainz.replication_control;" -c "SELECT now();"
 id | current_schema_sequence | current_replication_sequence |    last_replication_date
----+-------------------------+------------------------------+------------------------------
  1 |                      31 |                       187985 | 2026-08-04 20:01:47.70555+00

              now
 2026-08-04 20:19:00.461867+00
```

**Schema-Sequenz 31** — exakt der Wert, den `queries.py` als 2026er Stand erwartet.
Replikationsalter zum Messzeitpunkt: **~17 Minuten**. Damit liegt der Spiegel
weit innerhalb der geplanten Schwellen (WARN > 36 h, CRIT > 7 d).

---

## 3. Schema `cover_art_archive` — vorhanden, aber ohne Dateigrößen

```
$ ... -c "\dt cover_art_archive.*" -c "\dv cover_art_archive.*"
 cover_art_archive | art_type                | table
 cover_art_archive | cover_art               | table
 cover_art_archive | cover_art_type          | table
 cover_art_archive | image_type              | table
 cover_art_archive | release_group_cover_art | table
 cover_art_archive | index_listing           | view
```

Alle vier im Auftrag vermuteten Tabellen existieren, dazu
`release_group_cover_art` und die View `index_listing`.

```sql
SELECT n.nspname||'.'||c.relname, c.relkind, c.reltuples::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='cover_art_archive' AND c.relkind IN ('r','v');
```

| Objekt | est. Zeilen |
|---|---|
| `cover_art` | 7.369.111 |
| `cover_art_type` | 7.871.137 |
| `release_group_cover_art` | 23.040 |
| `art_type` | 18 |
| `image_type` | 4 |

`art_type` liefert die Typ-IDs; **Front = `id 1`**, Back = 2 (18 Typen gesamt).
`image_type` bildet MIME → Suffix ab: `image/jpeg`→`jpg`, `image/png`→`png`,
`image/gif`→`gif`, `application/pdf`→`pdf`.

### Struktur `cover_art` — was der Crawler bekommt

```
$ ... -c "\d cover_art_archive.cover_art"
 id                  | bigint                   | not null   <- CAA-Bild-ID (URL-Bestandteil)
 release             | integer                  | not null   -> musicbrainz.release.id
 comment             | text                     | not null
 edit                | integer                  | not null
 ordering            | integer                  | not null
 date_uploaded       | timestamp with time zone | not null
 edits_pending       | integer                  | not null
 mime_type           | text                     | not null
 filesize            | integer                  |
 thumb_250_filesize  | integer                  |
 thumb_500_filesize  | integer                  |
 thumb_1200_filesize | integer                  |
Indexes:
    "cover_art_pkey" PRIMARY KEY, btree (id)
    "cover_art_idx_release" btree (release)
```

### ⚠️ Befund: alle `filesize`-Spalten sind leer

```sql
SELECT count(*) AS zeilen, count(filesize) AS mit_filesize,
       count(thumb_250_filesize), count(thumb_500_filesize),
       count(thumb_1200_filesize) FROM cover_art_archive.cover_art;
```
```
 zeilen  | mit_filesize | mit_thumb250 | mit_thumb500 | mit_thumb1200 | pct_filesize
---------+--------------+--------------+--------------+---------------+--------------
 7383624 |            0 |            0 |            0 |             0 |          0.0
```

**0 von 7.383.624 Zeilen** haben eine Dateigröße — in keiner der vier Spalten.
Das ist kein Defekt des Spiegels, sondern normales Verhalten: die
Größen werden erst von der CAA-eigenen Infrastruktur nachgetragen und sind in den
öffentlichen Replikationspaketen nicht enthalten.

**Konsequenz für M5:** Der Crawler kann aus dem Spiegel **nicht** vorab wissen, wie
groß ein Bild ist. Für „welche Releases haben Front-Artwork" — die eigentliche
Verzeichnis-Funktion — ist das **irrelevant**; nur eine Größen-basierte Vorab-
Priorisierung oder Speicherplatz-Schätzung fällt weg (ersetzbar durch
`HEAD`-Request bzw. den `Content-Length` des ohnehin nötigen Downloads).

### ⚠️ Nebenbefund: View `index_listing` ist im Spiegel unbrauchbar

`pg_get_viewdef` zeigt `LEFT JOIN edit ON edit.id = cover_art.edit` und daraus
`edit.close_time IS NOT NULL AS approved`. Die Tabelle `musicbrainz.edit` ist im
Spiegel jedoch **leer** (`reltuples = 0` — Edit-Daten werden nicht repliziert),
also wäre `approved` immer `false`. **Empfehlung: `index_listing` nicht benutzen**,
sondern direkt gegen `cover_art` + `cover_art_type` joinen (so auch unten).

### Beispiel-Query „Releases mit Front-Artwork" — funktioniert

```sql
SELECT r.gid AS release_mbid, r.name, ca.id AS caa_image_id, ca.mime_type,
       ca.ordering, ca.date_uploaded
FROM cover_art_archive.cover_art ca
JOIN cover_art_archive.cover_art_type cat ON cat.id = ca.id AND cat.type_id = 1
JOIN musicbrainz.release r ON r.id = ca.release
ORDER BY ca.id LIMIT 5;
```
```
             release_mbid             |      name       | caa_image_id | mime_type
--------------------------------------+-----------------+--------------+------------
 32e3f28d-bd6b-45ae-a4e2-a3a453be523b | 超HAPPY SONG    |    829428054 | image/jpeg
 99632f13-e859-4138-a3f6-9f17d28e473a | Cara o cruz     |    829450710 | image/jpeg
 ...
Time: 14.601 ms
```

Aus `release_mbid` + `caa_image_id` lässt sich die CAA-URL direkt bauen
(`https://coverartarchive.org/release/{mbid}/{id}-1200.jpg`) — genau das, was das
Crawler-Verzeichnis braucht.

**Größe des Verzeichnisses** (einmaliger echter COUNT, 1,7 s):

```sql
SELECT count(DISTINCT ca.release) FROM cover_art_archive.cover_art_type cat
JOIN cover_art_archive.cover_art ca ON ca.id = cat.id WHERE cat.type_id = 1;
--  3760261      (Time: 1686.746 ms)
```

**3.760.261 Releases mit Front-Artwork** — bei 5.658.985 Releases gesamt sind das
**66,4 %**. Typ-Verteilung (Stichprobe 500 k aus `cover_art_type`): Front 254.979,
Booklet 82.468, Back 62.838, Medium 49.348, Spine 25.932.
MIME-Verteilung (Stichprobe 500 k): 96,6 % JPEG, 3,1 % PNG, Rest GIF/PDF.

---

## 4. URL-Relationships: MBID ↔ Discogs

```sql
SELECT c.relname, c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ...
WHERE n.nspname='musicbrainz' AND (relname LIKE 'l\_%\_url' OR relname IN ('url','link','link_type'));
```

| Tabelle | est. Zeilen |
|---|---|
| `l_release_url` | 10.264.212 |
| `l_artist_url` | 6.311.459 |
| `l_release_group_url` | 1.021.742 |
| `l_recording_url` | 4.590.355 |
| `l_label_url` | 391.636 |
| `url` | 20.884.416 |
| `link` | 1.136.213 |
| `link_type` | 697 |

Alle drei geforderten `l_*_url`-Tabellen sind vorhanden **und gefüllt**.

### Discogs-Link-Type-IDs

```sql
SELECT id, entity_type0, entity_type1, name, gid FROM musicbrainz.link_type
WHERE name ILIKE '%discogs%';
```

| ID | entity_type0 | entity_type1 | GID |
|---|---|---|---|
| **76** | **release** | url | `4a78823c-1c53-4176-a5f3-58026c76f2bc` |
| **90** | **release_group** | url | `99e550f3-5ab4-3110-b5b9-fe01d970b126` |
| **180** | **artist** | url | `04a5b104-a4c2-4bac-99a1-7b837c37d9e4` |
| **217** | **label** | url | `5b987f87-25bc-4a2d-b3f1-3618795b8207` |
| 705 / 747 / 971 / 1089 / 1275 | place / series / work / genre / event | url | — |

> **Hinweis für M7:** Nicht die numerischen IDs hart verdrahten, sondern über die
> **stabile `link_type.gid`** auflösen (einmal beim Start cachen). Die `id` ist
> eine lokale Sequenz, die `gid` ist der MusicBrainz-weit stabile Bezeichner.

### Beispiel-Query — funktioniert

```sql
SELECT r.gid AS release_mbid, r.name, u.url AS discogs_url
FROM musicbrainz.l_release_url lru
JOIN musicbrainz.link  l   ON l.id = lru.link AND l.link_type = 76
JOIN musicbrainz.url   u   ON u.id = lru.entity1
JOIN musicbrainz.release r ON r.id = lru.entity0
LIMIT 5;
```
```
             release_mbid             |             name             |               discogs_url
--------------------------------------+------------------------------+-----------------------------------------
 ede3df10-aca3-4c2c-ab6b-4d24700940d6 | Kiss Away                    | https://www.discogs.com/release/4063757
 842c09ad-7750-4a76-abd8-8392bcf29258 | Four Eyes In The Silence     | https://www.discogs.com/release/6924078
 ada8335b-c42a-3a0a-9b83-7ffedb87a83f | The Kick Inside              | https://www.discogs.com/release/1923128
 c4a928cf-c1e5-48f7-85c8-0cabbd639346 | Boulevard des hits, volume 8 | https://www.discogs.com/release/2223380
 7f762d9d-f1d2-4a7a-ab1b-5030a8dc0e61 | Foolish                      | https://www.discogs.com/release/897298
(5 rows)   Time: 12.460 ms
```

Artist-Variante (`link_type = 180`) ebenso geprüft: Massive Attack →
`https://www.discogs.com/artist/4480`. Die Discogs-ID ist per Suffix aus der URL
zu parsen; das Format ist einheitlich `https://www.discogs.com/{typ}/{id}`.

### Kombiniert (M5 + M7 in einer Query)

```sql
SELECT r.gid AS release_mbid, r.name, ca.id AS caa_image_id, ca.mime_type, u.url
FROM cover_art_archive.cover_art ca
JOIN cover_art_archive.cover_art_type cat ON cat.id = ca.id AND cat.type_id = 1
JOIN musicbrainz.release r         ON r.id = ca.release
JOIN musicbrainz.l_release_url lru ON lru.entity0 = r.id
JOIN musicbrainz.link l            ON l.id = lru.link AND l.link_type = 76
JOIN musicbrainz.url u             ON u.id = lru.entity1
LIMIT 5;
--  5 Zeilen, Time: 16.877 ms
```

### Laufzeitverhalten (relevant für `statement_timeout = 2 s`)

`EXPLAIN (COSTS OFF)` für den Einzel-Lookup per Release-MBID zeigt in **beiden**
Fällen reine Index-Zugriffe, keine Seq-Scans:

```
-- Discogs per Release-MBID
Nested Loop
  -> Index Scan using release_idx_gid on release r
  -> Index Only Scan using l_release_url_idx_uniq on l_release_url lru
  -> Index Scan using link_pkey on link l   (Filter: link_type = 76)
  -> Index Scan using url_pkey on url u

-- Front-Cover per Release-MBID
Nested Loop
  -> Index Scan using release_idx_gid on release r
  -> Index Scan using cover_art_idx_release on cover_art ca
  -> Index Only Scan using cover_art_type_pkey on cover_art_type cat
```

Gemessene Laufzeiten 0,7–17 ms. Der geplante `statement_timeout` von 2 s ist für
diese Zugriffsmuster großzügig bemessen. Vorhandene Indizes:
`l_release_url_idx_uniq (entity0, entity1, link, link_order)` und
`cover_art_idx_release (release)` decken die Lookup-Richtung „per Release" ab —
**keine zusätzlichen Indizes nötig** (die wir ohnehin nicht anlegen dürften).

---

## 5. Rollen & Grants — hier klemmt es

```
$ ... psql -U musicbrainz -d musicbrainz_db -c "\du"
                              List of roles
  Role name  |                         Attributes
-------------+------------------------------------------------------------
 musicbrainz | Superuser, Create role, Create DB, Replication, Bypass RLS
```

**Es gibt genau eine Rolle: `musicbrainz` (Superuser).** Die in
`phase1-mb-schema.md` (Zeilen 96–106), `ARCHITECTURE.md:467` und
`DECISIONS.md:408` vorgesehene Read-only-Rolle **`acoustid_ro` existiert nicht** —
das dokumentierte SQL-Snippet wurde nie ausgeführt.

Entsprechend ist auch nichts freigegeben:

```sql
SELECT nspname, nspacl FROM pg_namespace WHERE nspname IN ('musicbrainz','cover_art_archive','public');
--  musicbrainz       | (NULL)
--  cover_art_archive | (NULL)
--  public            | {pg_database_owner=UC/pg_database_owner,=U/pg_database_owner}

SELECT relacl FROM pg_class ... -- cover_art, cover_art_type, art_type,
                                -- recording, release, url, l_release_url, link, link_type
--  durchgaengig (NULL)

SELECT * FROM pg_default_acl;   --  (0 rows)
```

`NULL`-ACL heißt: **nur der Eigentümer** (`musicbrainz`) hat Zugriff, keine
`PUBLIC`-Grants, keine Default-Privileges. Es müssen also **alle** GRANTs neu
gesetzt werden — nicht nur die für die neuen Schemas.

Aktueller Projektzustand passend dazu: das Runtime-Config-Volume ist leer
(`/var/lib/docker/volumes/acoustid-offline_watchdog-data/_data/` → `total 0`),
`mb.dsn` ist also nie gesetzt worden. Das Projekt hat den Spiegel bislang **nie**
kontaktiert; die Metadaten-Anbindung (a) ist gebaut, aber noch nicht verdrahtet.

### Fertiges GRANT-Skript für den Betreiber

Gegenüber dem Snippet in `phase1-mb-schema.md` erweitert um `cover_art_archive`
und um den korrigierten Datenbanknamen. Einmalig als `musicbrainz` ausführen:

```bash
docker exec -it musicbrainz-docker-db-1 psql -U musicbrainz -d musicbrainz_db
```

```sql
-- 1) Rolle (Passwort bitte selbst setzen, nicht aus der Doku uebernehmen)
CREATE ROLE acoustid_ro LOGIN PASSWORD '<GEHEIM>';

GRANT CONNECT ON DATABASE musicbrainz_db TO acoustid_ro;

-- 2) Metadaten (a) — 375 Tabellen + 10 Views
GRANT USAGE  ON SCHEMA musicbrainz TO acoustid_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA musicbrainz TO acoustid_ro;

-- 3) Cover-Verzeichnis (b) — 5 Tabellen + 1 View   [NEU fuer M5]
GRANT USAGE  ON SCHEMA cover_art_archive TO acoustid_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA cover_art_archive TO acoustid_ro;

-- 4) Sitzungs-Defaults (Guerteldefensive: read-only, kurzer Timeout)
ALTER ROLE acoustid_ro SET search_path TO musicbrainz, cover_art_archive, public;
ALTER ROLE acoustid_ro SET default_transaction_read_only = on;
ALTER ROLE acoustid_ro SET statement_timeout = '2s';
ALTER ROLE acoustid_ro SET idle_in_transaction_session_timeout = '5s';

-- 5) Kuenftige Tabellen automatisch mitfreigeben (Schema-Upgrades!)
ALTER DEFAULT PRIVILEGES IN SCHEMA musicbrainz       GRANT SELECT ON TABLES TO acoustid_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA cover_art_archive GRANT SELECT ON TABLES TO acoustid_ro;
```

Verifikation nach dem Lauf (ebenfalls read-only):

```sql
SELECT has_schema_privilege('acoustid_ro','cover_art_archive','USAGE')  AS caa_usage,
       has_table_privilege ('acoustid_ro','cover_art_archive.cover_art','SELECT') AS caa_select,
       has_table_privilege ('acoustid_ro','musicbrainz.l_release_url','SELECT')   AS lru_select,
       has_table_privilege ('acoustid_ro','musicbrainz.url','SELECT')             AS url_select;
-- alle vier muessen t liefern
```

> **Wichtig:** `ALTER DEFAULT PRIVILEGES` wirkt nur für Tabellen, die **die
> ausführende Rolle** künftig anlegt. Die MB-Schema-Upgrades laufen als
> `musicbrainz` — das passt hier, weil das Skript als `musicbrainz` läuft. Das
> Runbook „nach Schema-Upgrades GRANT SELECT erneuern"
> (`phase1-mb-schema.md:107`) bleibt trotzdem als Absicherung sinnvoll, jetzt für
> **beide** Schemas.

### ⚠️ Zusätzlich blockierend: es gibt keinen Netzwerkpfad

```
$ ssh Tower 'docker port musicbrainz-docker-db-1'
(leer = kein Host-Port)

$ docker inspect musicbrainz-docker-db-1 --format "{{json .NetworkSettings.Networks}}"
"musicbrainz-docker_default": { "Aliases":["musicbrainz-docker-db-1","db"], "IPAddress":"172.22.0.3" }

$ docker inspect acoustid-db --format "{{json .NetworkSettings.Networks}}"
"acoustid-offline_default":  { "Aliases":["acoustid-db","db"], "IPAddress":"172.23.0.3" }
```

Die MB-Datenbank **veröffentlicht keinen Host-Port** (`5432/tcp` ist nur
`expose`), und die beiden Stacks liegen in **getrennten Docker-Netzen**
(172.22.0.0/16 vs. 172.23.0.0/16). Selbst mit korrekter Rolle käme das Projekt
derzeit nicht an die Datenbank heran.

> **Namenskollision beachten:** In *beiden* Netzen existiert der Alias **`db`**.
> Wird der Projekt-Container zusätzlich ins MB-Netz gehängt, ist `db` mehrdeutig
> und zeigt womöglich auf die falsche Datenbank. Im DSN daher **immer**
> `host=musicbrainz-docker-db-1` verwenden, **nie** `host=db`.

---

## 6. Replikation

```
$ docker exec musicbrainz-docker-musicbrainz-1 crontab -l
SHELL=/bin/bash
BASH_ENV=/noninteractive.bash_env
# Stuendlich statt taeglich: MusicBrainz veroeffentlicht stuendliche Pakete.
# Bei taeglichem Lauf (Standard 0 3 * * *) holt der Spiegel pro Tag nur EIN
# Paket auf und faellt dauerhaft zurueck, statt aufzuschliessen.
17 * * * * /usr/local/bin/replication.sh
```

Der Spiegel repliziert **stündlich zur Minute :17**, nicht täglich. Der Betreiber
hat den musicbrainz-docker-Standard (`0 3 * * *`) bewusst geändert.

> **Doku-Korrektur:** `docs/research/phase1-mb-schema.md:114` behauptet
> „Replikation: täglicher Cron (03:00) → Spiegel hinkt bis 24 h nach". Das ist
> **überholt**. Die Staleness-Schwellen (WARN > 36 h, CRIT > 7 d) bleiben als
> Ausfallerkennung gültig, sind gegenüber der Realität (~1 h) aber sehr locker.

### Wird `cover_art_archive` mitrepliziert? — Ja, Beleg:

```sql
SELECT max(date_uploaded) FROM cover_art_archive.cover_art;
--  2026-08-04 20:01:47.177138+00     (Time: 600 ms)

SELECT last_replication_date FROM musicbrainz.replication_control;
--  2026-08-04 20:01:47.705550+00

SELECT count(*) FROM cover_art_archive.cover_art WHERE date_uploaded > now() - interval '7 days';
--  22398
```

Der jüngste CAA-Upload trägt **denselben Zeitstempel wie die letzte Replikation**
(auf die halbe Sekunde). `cover_art_archive` läuft also im selben
Replikationsstrom mit; **22.398 neue Cover in den letzten 7 Tagen** (~3.200/Tag)
bestätigen laufende Aktualisierung. Der Crawler bekommt neue Releases damit
binnen einer Stunde ins Verzeichnis.

Ergänzend: `dbmirror2.pending_data` = 0 Zeilen (keine hängenden Pakete) —
der Spiegel ist sauber durchrepliziert, nicht mitten in einem Lauf steckengeblieben.

---

## Offene Punkte (Optionen + Empfehlung)

### OP-1 — Netzwerkpfad Projekt → MB-Datenbank

Ohne Pfad kein M5/M7. Drei Wege:

1. **Projekt-Stack ins MB-Netz hängen** (`networks:` um
   `musicbrainz-docker_default` als `external` erweitern), DSN
   `host=musicbrainz-docker-db-1 port=5432`. Kein offener Port auf dem Host,
   keine Änderung am MB-Stack. Verlangt den expliziten Container-Hostnamen wegen
   der `db`-Alias-Kollision.
2. **Host-Port veröffentlichen** über
   `admin/configure add publishing-db-port` mit
   `MUSICBRAINZ_DOCKER_HOST_IPADDRCOL=127.0.0.1:`, DSN dann
   `host=172.17.0.1`. Ändert den MB-Stack und erfordert dessen Neustart;
   exponiert die DB — bei fehlendem `127.0.0.1:`-Präfix sogar netzweit.
3. **MB-Netz an den Projekt-Stack anhängen** (umgekehrte Richtung, via
   `docker network connect`). Funktioniert, ist aber nicht im Compose
   festgeschrieben und überlebt ein `docker compose up -d` nicht zuverlässig.

> ✅ **Empfehlung: Option 1.** Rein additiv auf unserer Seite, der MB-Stack bleibt
> unangetastet (kein Neustart des 69-GB-Spiegels), nichts wird auf dem Host
> exponiert, und es ist deklarativ im Compose verankert. Einzige Auflage:
> im DSN `musicbrainz-docker-db-1` statt `db` verwenden.

### OP-2 — Fehlende Dateigrößen im CAA-Verzeichnis

`filesize` und alle Thumb-Größen sind zu 100 % NULL.

1. **Größen ignorieren**, Verzeichnis nur aus `release`/`id`/`mime_type`/
   `ordering` bauen; die tatsächliche Größe ergibt sich beim Download aus
   `Content-Length`.
2. **`HEAD`-Request je Kandidat** vor dem Download, um zu große Bilder zu
   überspringen. Verdoppelt die Requests gegen die CAA — genau die Last, die
   Risiko R3 („Upstream-Sperren") klein halten will.
3. **Spalten nachpflegen** aus einem CAA-Dump/der CAA-API. Zusätzlicher
   Import-Pfad und Schreibzugriff auf ein fremdes Schema — steht dem
   Read-only-Prinzip entgegen.

> ✅ **Empfehlung: Option 1.** Für die Verzeichnisfunktion („welche Releases haben
> Front-Artwork") sind Größen entbehrlich; ein Größenlimit lässt sich beim
> Download streamend durchsetzen (Abbruch bei Überschreitung), ohne einen zweiten
> Request. Option 2 bleibt als späteres Add-on möglich, falls Bandbreite zum
> Problem wird.

### OP-3 — Bezug der Discogs-Link-Types

Numerische IDs (76/90/180/217) sind lokale Sequenzwerte.

1. **Über `link_type.gid` auflösen**, einmalig beim Start cachen; GIDs sind
   MusicBrainz-weit stabil.
2. **IDs hart verdrahten** — heute korrekt, still falsch, falls sich die Sequenz
   je unterscheidet.

> ✅ **Empfehlung: Option 1**, plus Aufnahme von `link_type` und `link` in den
> `mb_selfcheck` (`EXPECTED_COLUMNS`), damit ein Schema-Drift laut auffällt statt
> leere Ergebnisse zu liefern.

### OP-4 — Doku-Nachzüge

- `docs/research/phase1-mb-schema.md:114` — Replikation ist **stündlich (:17)**,
  nicht täglich 03:00.
- Beispiel-DSN überall auf **`dbname=musicbrainz_db`** ziehen (die DB
  `musicbrainz` ist leer); betrifft die Betreiber-Doku, nicht zwingend die Tests
  in `shared/tests/test_config_schema.py:229` / `test_config_io.py:42`, die nie
  verbinden.
- GRANT-Snippet in `phase1-mb-schema.md` / `ARCHITECTURE.md:467` um
  `cover_art_archive` erweitern (Fassung oben in §5).
- `phase1-mb-schema.md` Offener Punkt 4 ist **erledigt**: View `release_event`
  und die Mirror-Indizes existieren.

> ✅ **Empfehlung:** Als Kleinst-Posten im laufenden Doku-Sweep miterledigen —
> kein eigenes Arbeitspaket.

---

## Fazit für das Gate

**Inhaltlich ist R19 entkräftet:** `cover_art_archive` und die
Discogs-URL-Relationships sind im Spiegel vorhanden, gefüllt, stündlich aktuell
und über indexgestützte Queries im Millisekundenbereich abfragbar. M5 und M7
können auf dem lokalen Spiegel aufsetzen.

**Betrieblich fehlen drei Dinge**, alle ohne Code-Änderung lösbar:
die Rolle `acoustid_ro` samt GRANTs (Skript in §5, Betreiber führt es einmalig
aus), der Netzwerkpfad (OP-1, Compose-Änderung auf unserer Seite) und die
Einplanung der fehlenden Dateigrößen (OP-2, Design-Entscheid).

Die einzige echte Funktionslücke gegenüber der HANDOFF-Annahme ist
`cover_art.filesize` — sie trifft aber nur die Priorisierung, nicht die
Verzeichnisfunktion selbst.
