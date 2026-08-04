# M4/M5 — Cover Art Archive: Bulk-Bezugsweg, URL-Schema, Crawl-Rate, Mengengerüst

**Recherche-Datum:** 2026-08-04 · **Auftraggeber-Kontext:** `docs/HANDOFF.md` (v2) §4, §6.3, §14.3, §15.1
**Ziel:** Gate für M4 (Cover-Subsystem) und M5 (CAA-Crawler) — lässt sich der Erst-Crawl
über einen offiziellen Bulk-Weg verkürzen?

Befunde aus eigenen Messungen sind mit **[EMPIRISCH]** markiert; alles andere stammt aus
der verlinkten Dokumentation. Alle Messungen erfolgten als Einzelabrufe (überwiegend HEAD,
also ohne Bildübertragung) gegen die echten Endpunkte; insgesamt rund 340 Requests.

---

## 0. Kurzfassung

1. **Einen Bulk-Download der Bilder gibt es nicht** — weder bei MetaBrainz noch beim
   Internet Archive (IA). Die IA-Collection `coverartarchive` ist `noindex`/`hidden`,
   damit versagen Such-, Scrape- und `ia download --search`-Wege **[EMPIRISCH]**.
2. **Der Bulk-Weg existiert für den *Index*, nicht für die *Bilder*.** Aus dem bereits
   vorausgesetzten MusicBrainz-Spiegel (Schema `cover_art_archive`) bzw. aus
   `mbdump-cover-art-archive.tar.bz2` (157 MiB) lässt sich die vollständige Arbeitsliste
   **offline** erzeugen — inklusive der Ziel-URL beim IA. Damit entfällt jeder Abruf gegen
   `coverartarchive.org` und es bleiben **1 statt 3 HTTP-Runden pro Cover**.
3. **Mengengerüst muss nach oben korrigiert werden:** nicht ~2 Mio., sondern
   **3.757.159 Releases mit Frontcover** (66,2 % von 5.678.979 Releases).
4. **Dateigrößen der 1200er-Variante:** Mittel 211 KiB, Median 148 KiB **[EMPIRISCH]** —
   die §15.1-Annahme „0,2–0,5 MB" ist am unteren Rand korrekt. Gesamt ≈ **0,76 TiB**.
5. **Größter operativer Befund:** die IA-Auslieferungsknoten liefern derzeit reproduzierbar
   **~30–50 % HTTP 500** auf nachweislich vorhandene Dateien — kein Rate-Limit, sondern
   defekte Knoten **[EMPIRISCH]**. Retry-Logik ist Pflicht, und 5xx darf **niemals** als
   „kein Cover" in den Negativ-Cache.

---

## 1. Bulk-Bezug

### 1.1 Wie sind die Daten beim IA organisiert?

**[EMPIRISCH]** Pro Release existiert genau **ein IA-Item** mit dem Identifier
`mbid-<release-mbid>`. Abfrage der Metadaten-API
(`https://archive.org/metadata/mbid-76df3287-6cda-33eb-8e9a-044b5e15ffdd`) ergab:

```
collection : coverartarchive
mediatype  : image
uploader   : caa@musicbrainz.org
noindex    : true
publicdate : 2012-04-30
files_count: 46      item_size: 8.557.177
d1 / d2    : ia601604.us.archive.org / ia801604.us.archive.org
dir        : /28/items/mbid-76df3287-6cda-33eb-8e9a-044b5e15ffdd
```

Je hochgeladenem Bild liegen im Item das Original plus vier Derivate:

| Datei | Bedeutung | Beispielgröße |
|---|---|---|
| `mbid-<mbid>-<coverid>.jpg` | Original (unverändert) | 76.106 B |
| `…_thumb250.jpg` | IA-Derivat „JPEG 250px Thumb" | 10.703 B |
| `…_thumb500.jpg` | IA-Derivat „JPEG 500px Thumb" | 35.043 B |
| `…_thumb1200.jpg` | IA-Derivat „JPEG 1200px Thumb" | 40.684 B |
| `…_thumb.jpg` / `…_itemimage.jpg` | IA-interne Kachel | ~6 KB |

Dazu je Item: `index.json` (CAA-Metadaten, s. §2.4), `<identifier>_meta.xml`,
`<identifier>_mb_metadata.xml`, `<identifier>_files.xml`, `<identifier>_archive.torrent`.

**Gezielter Bezug nur des Frontcovers in genau einer Größe ist möglich** — die
Derivate sind einzeln adressierbar (§2). Das ist der entscheidende Punkt: es müssen
weder Originale noch Rückseiten/Booklets übertragen werden.

### 1.2 Geprüfte Bulk-Wege — alle untauglich

| Weg | Befund | Bewertung |
|---|---|---|
| **IA-Suche / Scrape-API** (`services/search/v1/scrape?q=collection:coverartarchive`) | **[EMPIRISCH]** liefert `total: 230` statt ~3,76 Mio. Grund: das Collection-Item trägt `noindex: true`, `hidden: true`; auch die Einzel-Items tragen `noindex: true`. `advancedsearch.php` bestätigt `numFound: 230`. | ❌ Enumeration unmöglich |
| **`ia download --search 'collection:coverartarchive'`** | Basiert auf derselben Suchmaschine → würde 230 Items ziehen. | ❌ |
| **`ia download --itemlist`** | Funktioniert, **braucht aber eine Item-Liste** — die liefert erst der MB-Index (§1.3). Zusätzlich lädt `ia download` per Default das ganze Item; nur mit `--glob="*_thumb1200.jpg"` gezielt. | ⚠️ nur als Transport, Liste kommt von uns |
| **`metamgr.php?f=exportIDs&w_collection=coverartarchive`** | Der einzige echte IA-Listen-Export. Laut CAA-Auditor-README „requires being logged in to an account with sufficient privileges" — Admin-Recht, für uns nicht verfügbar. | ❌ nicht zugänglich |
| **Torrents** | **[EMPIRISCH]** Per-Item-Torrent vorhanden (`…_archive.torrent`, 5.791 B, HTTP 200). Aber: ein Torrent **pro Release** (3,76 Mio. Torrents) und jeder umfasst das **komplette** Item (Originale + alle Derivate) — ein Vielfaches der benötigten Datenmenge. Kein Collection-Torrent. | ❌ kontraproduktiv |
| **rsync / IA-S3** | IA-S3 ist eine Upload-Schnittstelle; kein öffentlicher rsync-Spiegel der Collection dokumentiert. | ❌ |
| **Bild-Dump bei MetaBrainz** | **[EMPIRISCH]** `data.metabrainz.org/pub/musicbrainz/` enthält ausschließlich Daten-/Suchindex-Bestände (`data/`, `canonical_data/`, `search/`, `solr/`, `listenbrainz/`, …) — **kein** Bildbestand. | ❌ existiert nicht |

### 1.3 Der taugliche Weg: Index-Dump statt Bild-Dump

**[EMPIRISCH]** `https://data.metabrainz.org/pub/musicbrainz/data/fullexport/LATEST`
→ `20260801-002250`, darin:

| Datei | Größe |
|---|---|
| `mbdump-cover-art-archive.tar.bz2` | 164.374.473 B (157 MiB) |
| `mbdump.tar.bz2` (Kern-Dump, zum Vergleich) | 7.400.395.332 B |

Inhalt (per Teil-Download der ersten 32 MiB und lokalem Entpacken geprüft **[EMPIRISCH]**):

```
TIMESTAMP, COPYING, README, REPLICATION_SEQUENCE, SCHEMA_SEQUENCE
mbdump/cover_art_archive.art_type            2.027 B
mbdump/cover_art_archive.cover_art     651.982.450 B
(danach: cover_art_type, image_type, release_group_cover_art)
```

Spalten von `cover_art` (aus `admin/sql/caa/CreateTables.sql`):
`id, release, comment, edit, ordering, date_uploaded, edits_pending, mime_type,
filesize, thumb_250_filesize, thumb_500_filesize, thumb_1200_filesize`

`art_type`-IDs **[EMPIRISCH aus dem Dump]**: **1 = Front**, 2 = Back, 3 = Booklet,
4 = Medium, 5 = Obi, 6 = Spine, 7 = Track, 8 = Other, 9 = Tray, 10 = Sticker,
11 = Poster, 15 = Matrix/Runout.

> ⚠️ **Warnung — die vier `*_filesize`-Spalten sind im Dump zu 100,00 % NULL**
> **[EMPIRISCH]** (1.899.710 ausgewertete Zeilen, kein einziger Wert gesetzt).
> Die Spalten existieren seit dem Schema-Release 2019-05-13, wurden aber offenbar nie
> befüllt/exportiert. **Der Speicherbedarf lässt sich also nicht aus dem Dump berechnen** —
> daher die Stichprobenmessung in §4.2. Für M5 heißt das auch: die Vorab-Prüfung
> „passt das Cover ins Budget" ist nicht ohne Abruf möglich.

**Konsequenz:** Der Erst-Crawl braucht keinen Online-Verzeichnisaufbau. Die vollständige
Arbeitsliste (Release-MBID + Cover-ID + Ziel-URL) entsteht in **einer SQL-Abfrage** gegen
den ohnehin vorausgesetzten MB-Spiegel — sinngemäß:

```sql
SELECT r.gid AS release_mbid, ca.id AS cover_id
FROM   cover_art_archive.cover_art      ca
JOIN   cover_art_archive.cover_art_type cat ON cat.id = ca.id AND cat.type_id = 1  -- Front
JOIN   musicbrainz.release              r   ON r.id = ca.release
WHERE  ca.edits_pending = 0
ORDER  BY ca.ordering;   -- je Release das Bild mit kleinstem ordering nehmen
```

Daraus direkt konstruierbar:

```
https://archive.org/download/mbid-{release_mbid}/mbid-{release_mbid}-{cover_id}_thumb1200.jpg
```

**[EMPIRISCH]** verifiziert: die so konstruierte URL liefert HTTP 200 und exakt dieselbe
Datei wie der Umweg über `coverartarchive.org/release/{mbid}/front-1200`.

**Ersparnis:** 3 HTTP-Runden pro Cover (CAA-307 → archive.org-302 → Datenknoten-200)
schrumpfen auf 1 Runde (archive.org-302 → Datenknoten-200 = 2 Runden; bei d1/d2-Pinning
sogar 1). Zusätzlich entfällt jede Abhängigkeit von der CAA-Verfügbarkeit und deren
dokumentiertem 503-Rate-Limit.

**Nachzügler-Bonus:** Der MB-Spiegel repliziert stündlich. Neue/entfernte `cover_art`-Zeilen
sind damit ohne einen einzigen Netz-Abruf erkennbar — der tägliche Nachzügler wird zu einem
reinen Delta gegen die eigene Cover-Tabelle. **Wichtig:** auch **Löschungen** spiegeln
(DMCA-Takedowns, Merges), sonst behält der Spiegel entfernte Bilder.

---

## 2. URL- und Größenschema (exakt, empirisch geprüft)

Testobjekte: `76df3287-6cda-33eb-8e9a-044b5e15ffdd` (M1, Item von 2012, 6 Bilder),
`ff201379-b5f2-4575-8816-beff1ce26fb5` (M2) sowie 63 zufällig gezogene Releases (§4).

### 2.1 Redirect-Kette **[EMPIRISCH]**

```
GET https://coverartarchive.org/release/{mbid}/front-1200
  → 307  location: https://archive.org/download/mbid-{mbid}/mbid-{mbid}-{coverid}_thumb1200.jpg
  → 302  location: https://dn711103.ca.archive.org/0/items/mbid-{mbid}/…_thumb1200.jpg
  → 200  content-type: image/jpeg
```
`num_redirects = 2`, also **3 Requests pro Cover**. Der zweite Hop landet mal auf einem
`dnXXXXXX.ca.archive.org`-Knoten, mal direkt auf `ia6…`/`ia8…` (siehe §3.2).
Die 307-Antwort der CAA trägt `access-control-allow-origin: *` und hat keinen Body.

### 2.2 Größen-Suffixe **[EMPIRISCH]**

| Aufruf | Ziel-Datei |
|---|---|
| `/front` | `mbid-…-{coverid}.jpg` (Original) |
| `/front-250` | `…_thumb250.jpg` |
| `/front-500` | `…_thumb500.jpg` |
| `/front-1200` | `…_thumb1200.jpg` |
| `/back` | `mbid-…-{andere-coverid}.jpg` |

> ⚠️ **Fallstrick:** `/front-2000` und `/front-999` liefern **307 auf das Original** —
> keinen Fehler **[EMPIRISCH]**. Ein Tippfehler im Suffix führt also stillschweigend zum
> ungedrosselten Voll-Download (Mittel 662 KiB statt 211 KiB). Der Crawler muss das Suffix
> als Konstante führen und die Antwortgröße plausibilisieren.

**Existenzgarantie der 1200er-Variante:** Die offizielle API-Doku sagt ausdrücklich
*„This redirected request may resolve to a 404 if the thumbnail does not exist."* — es gibt
also **keine** dokumentierte Garantie und **keinen** automatischen Fallback aufs Original.
**[EMPIRISCH]** liegt die 1200er-Variante praktisch aber flächendeckend vor: das
IA-Metadata-Listing des 2012er-Items zeigt `_thumb1200.jpg` für **alle sechs** Bilder
(IA hat rückwirkend abgeleitet), und in der 63er-Zufallsstichprobe gab es **keinen einzigen
404 auf `front-1200` bei vorhandenem Frontcover** (die eine Abweichung war ein 500, s. §3.2).

→ **Fallback trotzdem implementieren:** bei 404 auf `-1200` einmal `/front` (Original)
holen und lokal auf 1200 px skalieren.

### 2.3 HTTP-Statuscodes **[EMPIRISCH]** vs. Doku

| Fall | gemessen | Doku |
|---|---|---|
| Frontcover vorhanden | **307** | 307 |
| gültige UUID, kein Release / kein Front | **404** | 404 |
| syntaktisch ungültige MBID (`nicht-eine-uuid`) | **400** | 400 |
| `/release/{mbid}/` (Listing) | **307** → `…/index.json` | 307 |
| Rate-Limit | **nie gesehen** (0 × in ~340 Requests) | 503 (CAA), 429 (IA) |
| Auslieferungsfehler | **500** (nginx, 170 B HTML-Body) | nicht dokumentiert |

Merke: `404` = „kein Cover" (Negativ-Cache berechtigt).
`400` = Programmierfehler bei uns. `500` = **Infrastruktur**, niemals Negativ-Cache.

### 2.4 `index.json`

`GET /release/{mbid}/` → 307 → `https://archive.org/download/mbid-{mbid}/index.json`.
Struktur **[EMPIRISCH]**: `{"images":[{"id","front","back","types","approved","comment",
"image","thumbnails":{"250","500","1200","small","large"},…}]}`. Die `thumbnails`-URLs
zeigen zurück auf `coverartarchive.org` (noch mit `http://`, nicht `https://`).
Für uns nur als Notnagel interessant — der MB-Spiegel liefert dieselbe Information offline.

---

## 3. Verträgliche Crawl-Rate

### 3.1 Dokumentierte Vorgaben

**[EMPIRISCH] `https://coverartarchive.org/robots.txt`:**
```
User-agent: *
Allow: /
```
→ Vollständig freigegeben, keine Einschränkung.

**[EMPIRISCH] `https://archive.org/robots.txt`:** nur `Disallow: /control/` und
`Disallow: /report/`. `/download/` und `/metadata/` sind ausdrücklich **nicht** gesperrt.

**CAA-API-Doku:** *„There are currently no rate limiting rules in place"* — gleichzeitig
ist aber je Endpunkt `503 (rate limit exceeded)` als möglicher Code gelistet. Also:
aktuell keine Regel, aber die Mechanik ist vorhanden und kann jederzeit scharf geschaltet
werden.

**IA-Entwicklerdoku (`archive.org/developers/iarest.html`):** `429 Too Many Requests` ist
vorgesehen — *„The client should take a breather and try again later"*; ein `Retry-After`
darf, muss aber nicht mitkommen. `X-Accept-Reduced-Priority: 1` betrifft nur die
Tasks-/Metadata-API, nicht Downloads. **Konkrete Zahlen (req/s, req/Tag) nennt das IA
nirgends.** Die kursierenden „15/Minute" beziehen sich auf *Save Page Now*, nicht auf
`/download/`.

**Bewertung:** Der v2-Default von **2 Anfragen/s ist realistisch und konservativ**. In rund
340 Requests (teilweise mit ~2/s) trat **kein einziges 429 oder 503** auf **[EMPIRISCH]**.
Der begrenzende Faktor ist nicht die Höflichkeit, sondern die Fehlerrate der IA-Knoten (§3.2).

### 3.2 ⚠️ Hauptbefund: transiente HTTP 500 der Auslieferungsknoten

**[EMPIRISCH]** Testdatei: `mbid-76df3287-…-829521842_thumb500.jpg`, laut IA-Metadata-API
**nachweislich vorhanden** (35.043 B).

| Messreihe | Rate | Ergebnis |
|---|---|---|
| 20 × über `archive.org/download/…` | ~1,6/s | **10 × 200, 10 × 500** (50 % Fehler) |
| 8 × über `archive.org/download/…` | **0,2/s** | **5 × 200, 3 × 500** (37,5 % Fehler) |
| 10 × direkt `ia601604.us.archive.org` (= `d1`) | 2/s | **10 × 200, 0 Fehler** |
| 10 × direkt `ia801604.us.archive.org` (= `d2`) | 2/s | **10 × 200, 0 Fehler** |
| 10 × ganze Kette `coverartarchive.org/front-500` | 2/s | 6 × 200, 4 × 500 |

Fehlerantwort: `HTTP/2 500`, `server: nginx`, `content-length: 170`, Body
`<html><head><title>500 Internal Server Error</title>…`. Auffällig: der `content-type:
image/jpeg` stammt noch aus der 302-Antwort — **ein naiver Client, der nur den Content-Type
prüft, schreibt sich 170-Byte-Müll als „JPEG" in den Spiegel.** Größenprüfung ist Pflicht.

**Deutung:** Die Fehler kamen ausschließlich von `dnXXXXXX.ca.archive.org`-Knoten, auf die
`archive.org/download/…` per 302 verteilt. Die im Item-Metadatensatz genannten Knoten
`d1`/`d2` lieferten **20/20 fehlerfrei**. Da die Fehlerquote bei 0,2/s praktisch unverändert
bleibt, ist es **kein Rate-Limit**, sondern ein degradierter Auslieferungs-Layer.

> **Vorbehalt:** Momentaufnahme eines Tages von einem Standort. Die `dn*`-Knoten werden
> vermutlich geografisch zugeteilt; auf der Unraid-Kiste kann die Quote anders sein.
> **Vor dem Erst-Crawl mit einer 200er-Stichprobe neu messen** und in die Betriebsdoku
> aufnehmen.

**Retry-Wirksamkeit [EMPIRISCH]** (127 Messungen der Zufallsstichprobe, bis zu 3 Versuche
mit 1 s Pause): 88 beim 1. Versuch (69,3 %), 23 beim 2., 16 beim 3. — **9 von 127 (7,1 %)
scheiterten auch nach 3 Versuchen.** Ein Retry hilft, weil er neu aufgelöst wird und oft auf
einem anderen Knoten landet; kurze Pausen reichen aber nicht.

### 3.3 Empfohlenes Verhalten des Crawlers

1. **Drossel zählt Cover, nicht HTTP-Runden** — sonst verdreifacht die Redirect-Kette die
   effektive Laufzeit.
2. **Retry** bei 5xx und Netzfehlern: 5 Versuche, exponentieller Backoff mit Jitter
   (1 s → 4 s → 15 s → 60 s → 240 s), **Retry zählt nicht gegen den Negativ-Cache**.
3. **Härtung (empfohlen, aber optional):** nach 2 vergeblichen Versuchen einmal
   `https://archive.org/metadata/mbid-{mbid}` abrufen und `https://{d1}{dir}/{datei}`
   direkt ansprechen. **[EMPIRISCH]** 100 % Trefferquote in der Messreihe.
4. **429/503 respektieren:** `Retry-After` auswerten, sonst Drossel selbsttätig halbieren,
   Ereignis-Log + Notification (deckt sich mit HANDOFF §12.6 „Bei 429/Sperren:
   exponentielles Backoff, Ereignis-Log").
5. **Plausibilitätsprüfung jeder Datei:** Statuscode 200 **und** `Content-Length` > ~1 KiB
   **und** JPEG-Magic `FF D8 FF` — sonst als Fehlversuch behandeln.
6. **User-Agent** mit Projektname, Version und Kontaktadresse.
7. **Eine Verbindung pro Zielhost**, keine Parallel-Sessions gegen `archive.org`.

---

## 4. Mengengerüst

### 4.1 Anzahl

**Offizielle MusicBrainz-Statistik, Abruf 2026-08-04 [EMPIRISCH]**
(`https://musicbrainz.org/statistics`, Block „Cover art sources"):

| Kennzahl | Wert |
|---|---|
| Releases gesamt | **5.678.979** |
| davon mit Frontcover in der CAA | **3.757.159 (66,2 %)** |
| ohne Frontcover | 1.921.820 (33,8 %) |

**Gegenprobe [EMPIRISCH]:** In einer Stichprobe von 63 zufällig über die MusicBrainz-Suche
gezogenen Releases hatten **39 = 61,9 %** ein Frontcover — deckt sich mit den 66,2 %
im Rahmen der Stichprobenstreuung.

**Bilder insgesamt (alle Typen) [EMPIRISCH]:** Der ausgewertete Dump-Anteil (25,5 % von
`cover_art`, 1.899.710 Zeilen) hochgerechnet ⇒ **≈ 7,44 Mio. `cover_art`-Zeilen**, verteilt
auf **2,04 Bilder je Release**. MIME-Verteilung: 93,96 % `image/jpeg`, 5,70 % `image/png`,
0,22 % `application/pdf`, 0,11 % `image/gif`.

> ⚠️ **Korrektur zu HANDOFF §4 / §15.1:** Dort steht „~2 Mio. Cover". Der reale Wert ist
> **3,76 Mio.** — Faktor **1,88**. Betrifft sowohl die Crawl-Dauer als auch den
> Speicherbedarf. Die 2-Mio.-Zahl entspricht in etwa dem Stand von ~2016.

### 4.2 Dateigrößen **[EMPIRISCH]**

Da die `*_filesize`-Spalten im Dump durchweg NULL sind (§1.3), gemessen über
`Content-Length` der finalen Antwort bei 63 zufälligen Releases (nur HEAD, keine
Bildübertragung):

| | n | Mittel | Median | p10 | p25 | p75 | p90 | max |
|---|---|---|---|---|---|---|---|---|
| **`front-1200`** | 38 | **211 KiB** | 148 KiB | 49 | 83 | 319 | 397 KiB | 0,56 MiB |
| `front` (Original) | 39 | 662 KiB | 375 KiB | 44 | 117 | 673 | 1.528 KiB | 7,65 MiB |

Mittleres Verhältnis 1200er/Original: **0,71**.

> **Auffälligkeit:** In **8 von 38 Fällen ist die 1200er-Variante *größer* als das Original**
> **[EMPIRISCH]** — nämlich dann, wenn das Original bereits kleiner als 1200 px ist und das
> IA es mit hoher Qualität neu kodiert. Betrifft kleine Dateien, der absolute Mehrverbrauch
> ist gering (s. §7, Frage 4).

**Bewertung der §15.1-Annahme „0,2–0,5 MB":** Der gemessene **Mittelwert 0,216 MB** liegt
exakt an der unteren Kante des angenommenen Bandes. Die Annahme ist also plausibel, aber
leicht pessimistisch.

### 4.3 Hochrechnung

| Größe | Rechnung | Ergebnis |
|---|---|---|
| **Speicher CAA-Spiegel** | 3.757.159 × 211 KiB | **≈ 756 GiB (812 GB)** |
| — Bandbreite der Schätzung (n=38, rechtsschiefe Verteilung) | | **0,6 – 1,0 TB** |
| — zum Vergleich Median-basiert | 3.757.159 × 148 KiB | 530 GiB |
| — zum Vergleich Originale statt 1200er | 3.757.159 × 662 KiB | 2,37 TiB |
| **Erst-Crawl bei 2/s, ohne Retrys** | 3.757.159 / 2 | **21,7 Tage** |
| **Erst-Crawl bei 2/s, mit ~30 % Retry-Aufschlag** | | **≈ 28–31 Tage** |
| Erst-Crawl bei 5/s (+Retrys) | | ≈ 11–12 Tage |
| Erst-Crawl bei 10/s (+Retrys) | | ≈ 6 Tage |
| **Netzlast bei 2 Cover/s** | 2 × 211 KiB/s | **≈ 3,5 Mbit/s** |
| Netzlast bei 5 Cover/s | | ≈ 8,6 Mbit/s |

**Netto:** §15.1s Gesamtband „500 GB – 1,5 TB" bleibt gültig — beide Eingangsgrößen waren
falsch, aber gegenläufig (Anzahl zu niedrig, Stückgröße zu hoch). §4s „2–4 Wochen" ist bei
2/s dagegen **zu optimistisch**; realistisch sind **4–4,5 Wochen** — außer die Drossel wird
angehoben.

**Zum Einordnen:** Ein Bulk-Download hätte hier ohnehin wenig geholfen — die 756 GiB müssen
so oder so über die Leitung, und bei 3,5 Mbit/s ist nicht die Rate das Nadelöhr, sondern die
Anzahl der Einzelabrufe. Genau die halbiert bis drittelt der Index-Weg aus §1.3.

---

## 5. Lizenz / ToS

- **Bilder sind nicht frei lizenziert.** `coverartarchive.org` stellt fest:
  *„All images are copyrighted by their respective copyright owners."* Es gibt **keine**
  CC-Lizenz — anders als bei den MusicBrainz-*Daten* (CC0 für die Kerndaten).
- **MusicBrainz-Doku:** *„Use the images at your own risk. The Internet Archive's policy can
  be read here"* und *„Be respectful of the rights of the artists and labels."* MetaBrainz
  gibt ausdrücklich **keine** Rechtsauskunft.
- **Es gibt kein Verbot des Spiegelns** — weder in `robots.txt` (beide erlauben den Zugriff,
  §3.1) noch in der CAA-Doku noch in den IA-Nutzungsbedingungen. Der Bezug selbst ist also
  vorgesehen und erlaubt; die Verantwortung für die **Verwendung** liegt beim Abrufer.
- **Takedowns:** DMCA an `copyright@metabrainz.org`, Entfernung beim IA über
  `info@archive.org`.
- **Der Index-Dump** (`mbdump-cover-art-archive.tar.bz2`) enthält eine `COPYING`-Datei mit
  der MetaBrainz-Lizenz; für den Eigengebrauch unkritisch.

**Bewertung für dieses Projekt:** Die Projektregel „keine Weiterverteilung der
Datenbestände" (HANDOFF §6.3) deckt das Risiko vollständig ab — ein lokaler Spiegel für den
Eigengebrauch ist rechtlich derselbe Vorgang wie ein großer Cache. **Zwei Auflagen sollten
in die Doku:**
1. **Löschungen mitspiegeln** (§1.3) — wird ein Bild wegen eines Takedowns aus der CAA
   entfernt, muss es auch lokal verschwinden. Ohne das wird aus dem Cache ein Archiv, das
   der Rechteinhaber nicht mehr erreichen kann.
2. Der CAA-kompatible Endpunkt (HANDOFF §9) liefert nur ins **eigene LAN** — er darf nicht
   ins offene Internet gehängt werden, sonst wird aus „kein Weiterverteilen" schnell doch
   eine Weiterverteilung.

---

## 6. Konsequenzen für M4/M5 (Umsetzungshinweise)

**M4 — Cover-Subsystem**
1. Cover werden als **`_thumb1200.jpg` unverändert** abgelegt: bereits JPEG, bereits
   ≤ 1200 px lange Kante. **Kein Re-Encode** — spart CPU und eine zweite JPEG-Generation.
   Nur der 404-Fallback aufs Original (§2.2) und die Fremdquellen (TADB/Discogs) brauchen
   die Normalisierungsstufe.
2. Validierung jeder Datei: HTTP 200 **+** Länge > 1 KiB **+** JPEG-Magic `FF D8 FF` (§3.3).
3. Negativ-Cache **ausschließlich** bei 404. 5xx/Timeout → Wiedervorlage, nicht negativ.
4. Der Negativ-Cache wird für den CAA-Zweig fast überflüssig: aus dem MB-Spiegel ist a
   priori bekannt, welche Releases Frontcover haben. Er bleibt relevant für die
   Folgeglieder der Kette (TheAudioDB, Discogs).

**M5 — CAA-Crawler**
5. Verzeichnis per SQL aus dem MB-Spiegel (§1.3), **kein** Online-Verzeichnisaufbau.
   Cursor in `crawl_state` über `cover_art.id` (aufsteigend, stabil).
6. Ziel-URL direkt konstruieren; `coverartarchive.org` gar nicht erst anfragen.
7. Retry/Backoff und optionales d1/d2-Pinning nach §3.3.
8. Nachzügler als Delta gegen die eigene Cover-Tabelle, **inkl. Löschungen**.
9. Suffix `_thumb1200` als Konstante führen (§2.2-Fallstrick).
10. Vor Freigabe des Erst-Crawls eine 200er-Stichprobe fahren und Fehlerquote + mittlere
    Dateigröße gegen §3.2/§4.2 gegenprüfen; erst dann die Laufzeit zusagen.

**Doku-Nachzug (HANDOFF)**
11. §4: „~2 Mio. Cover" → **3,76 Mio.**; „2–4 Wochen" → **4–4,5 Wochen bei 2/s**.
12. §14.3: Recherchepunkt als erledigt markieren, Ergebnis „kein Bild-Bulk, aber
    Index-Bulk" eintragen.
13. §15.1: Zeile auf „~3,76 Mio. Cover à ~0,21 MB → ~0,8 TB" präzisieren
    (Gesamtsumme Array bleibt unverändert).

---

## 7. Offene Fragen — Optionen + Empfehlung

**Frage 1 — Bezugsweg des Crawlers**
- **(a) ✅ Empfohlen: Direkte IA-URL aus dem MB-Spiegel.** 1–2 statt 3 HTTP-Runden, keine
  CAA-Abhängigkeit, Verzeichnis offline. Kosten: eine SQL-Abfrage mehr, Kopplung an das
  `cover_art_archive`-Schema (die HANDOFF §14.5 ohnehin vorsieht).
- (b) Über `coverartarchive.org/release/{mbid}/front-1200` wie in v2 skizziert. Einfacher,
  aber 3 Runden je Cover und abhängig vom CAA-Rate-Limit.
- (c) Hybrid: (a) für den Erst-Crawl, (b) als Lazy-Fallback für einzelne Cover.
  → *In der Praxis läuft (a) auf (c) hinaus, weil der Lazy-Pfad ohnehin MBIDs ohne
  Spiegel-Treffer bedienen muss.* Empfehlung: **(a) für den Crawler, (b) für den
  Lazy-Fallback** — also faktisch (c).

**Frage 2 — Crawl-Rate**
- **(a) ✅ Empfohlen: Default bei 2/s belassen, Obergrenze 5/s konfigurierbar.** Der
  Betreiber kann beim Erst-Crawl auf 5/s gehen (≈ 11 Tage statt ≈ 30) und danach
  zurückdrehen. Deckt sich mit „konservative Repo-Defaults" (HANDOFF §3).
- (b) Default auf 5/s anheben. Schneller, aber ein aggressiver Default widerspricht der
  Projektlinie und dem Risiko „Upstream-Sperren" (§15.2.3).
- (c) Bei 2/s bleiben, hart. Ehrlichste Zahl, aber ~4,5 Wochen Array-Dauerbetrieb.

**Frage 3 — Umgang mit den 500ern**
- **(a) ✅ Empfohlen: Retry mit exponentiellem Backoff (5 Versuche) + Größen-/Magic-Prüfung.**
  Deckt ~93 % ab, minimaler Aufwand.
- (b) Zusätzlich d1/d2-Pinning über die IA-Metadata-API nach 2 Fehlversuchen. **[EMPIRISCH]**
  100 % Trefferquote, kostet einen Metadata-Abruf je Problemfall. → Als **Ausbaustufe in M5
  einplanen**, scharf schalten, falls die Stichprobe vor dem Erst-Crawl > 10 % Fehler zeigt.
- (c) Nur Retry ohne Prüfung. **Abzulehnen** — schreibt 170-Byte-HTML als „JPEG" in den
  Spiegel (§3.2).

**Frage 4 — Wenn die 1200er-Variante größer ist als das Original (8/38 Fälle)**
- **(a) ✅ Empfohlen: Ignorieren, immer `_thumb1200` nehmen.** Betrifft nur kleine Bilder;
  der Mehrverbrauch liegt im niedrigen einstelligen GB-Bereich über den Gesamtbestand. Ein
  Vergleich würde je Cover einen zusätzlichen HEAD kosten (+3,76 Mio. Requests).
- (b) Vorab beide Größen per HEAD vergleichen und die kleinere nehmen. Sauber, aber
  verdoppelt die Requests für < 1 % Ersparnis.
- (c) Original holen und selbst skalieren. Verdreifacht das Transfervolumen (2,37 TiB).

**Frage 5 — Zeitpunkt der Doku-Korrektur (§6, Punkte 11–13)**
- **(a) ✅ Empfohlen: Sofort nachziehen**, vor M4-Start — die Zahlen gehen in Speicher- und
  Zeitplanung ein, und die Doku-Regel verlangt Aktualität vor der nächsten Phase.
- (b) Erst mit dem M4-Abschluss. Spart einen Commit, riskiert aber, dass M4 gegen die alte
  2-Mio.-Annahme dimensioniert wird.

---

## 8. Quellen

**Dokumentation**
- Cover Art Archive – API: https://musicbrainz.org/doc/Cover_Art_Archive/API
- Cover Art Archive – Übersicht: https://musicbrainz.org/doc/Cover_Art_Archive
- Cover Art Archive – Startseite: https://coverartarchive.org/
- MusicBrainz-Statistik: https://musicbrainz.org/statistics
- MusicBrainz Copyright/DMCA: https://musicbrainz.org/doc/Copyright_and_DMCA_Compliance
- CAA-Schema (`CreateTables.sql`):
  https://raw.githubusercontent.com/metabrainz/musicbrainz-server/master/admin/sql/caa/CreateTables.sql
- Schema-Change 2019-05-13 (`*_filesize`-Spalten):
  https://blog.metabrainz.org/2019/03/07/schema-change-release-may-13-2019/
- IA REST-Microservices (429, `X-Accept-Reduced-Priority`):
  https://archive.org/developers/iarest.html
- IA Command-Line-Tool (`ia download --search/--itemlist/--glob`):
  https://archive.org/developers/internetarchive/cli.html
- IA-Collection „Cover Art Archive": https://archive.org/details/coverartarchive
- CAA-Auditor (Hinweis auf `metamgr.php?f=exportIDs`, Admin-Recht nötig):
  https://github.com/ROpdebee/CAA-Auditor

**Empirisch abgefragte Endpunkte**
- `https://coverartarchive.org/release/{mbid}/{front|back}[-250|-500|-1200|-999|-2000]`
- `https://coverartarchive.org/robots.txt`, `https://archive.org/robots.txt`
- `https://archive.org/download/mbid-{mbid}/…`, `…/index.json`, `…_archive.torrent`
- `https://archive.org/metadata/mbid-{mbid}`, `https://archive.org/metadata/coverartarchive`
- `https://archive.org/services/search/v1/scrape?q=collection:coverartarchive`
- `https://archive.org/advancedsearch.php?q=collection:coverartarchive`
- `https://data.metabrainz.org/pub/musicbrainz/data/fullexport/{LATEST}/…`
- `https://musicbrainz.org/ws/2/release/?query=*&limit=25&offset=…` (Stichprobenziehung)

---

## Anhang — Messprotokoll (Kurzfassung)

| # | Messung | Umfang | Kernergebnis |
|---|---|---|---|
| 1 | Größen-Suffixe an M1 | 6 HEAD | Abbildung auf `_thumb{250,500,1200}`; unbekannte Suffixe → Original |
| 2 | Volle Redirect-Kette | 1 GET | 2 Redirects, 3 Requests, `image/jpeg` |
| 3 | Fehlerfälle | 5 HEAD | 404 / 400 / 307 wie dokumentiert |
| 4 | `robots.txt` beider Dienste | 2 GET | beide erlauben den Zugriff |
| 5 | IA-Metadata M1 | 1 GET | 46 Dateien, 4 Derivate je Bild, `noindex:true`, d1/d2 |
| 6 | Scrape-/Advancedsearch-API | 2 GET | `total: 230` statt 3,76 Mio. → Collection nicht indiziert |
| 7 | Transiente 500er, schnell | 20 GET | 50 % Fehler auf vorhandene Datei |
| 8 | Transiente 500er, langsam | 8 GET | 37,5 % Fehler bei 0,2/s → kein Rate-Limit |
| 9 | d1/d2 direkt | 20 GET | 0 Fehler |
| 10 | Index-Dump, Teil-Download | 32 MiB Range | 1.899.710 Zeilen; alle `*_filesize` NULL; `art_type` 1 = Front |
| 11 | Zufallsstichprobe Releases | 63 Releases, 127 HEAD-Ketten | 61,9 % Abdeckung; `front-1200` Mittel 211 KiB; 7,1 % Restfehler nach 3 Versuchen |
| 12 | Alt-Item (2012) 1200er-Derivate | 3 HEAD + Metadata | `_thumb1200` für alle Bilder vorhanden (rückwirkend abgeleitet) |
