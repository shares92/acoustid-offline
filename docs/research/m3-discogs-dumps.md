# M3 — Discogs-Spiegel: Recherche (Gate für Phase M3)

**Stand:** 04.08.2026 · **Auftrag:** HANDOFF.md (v2) §4, §6.2, §8, §14.2, §15.1
· **Ziel:** Nachbau-Spezifikation für den Discogs-Dump-Spiegel + API-kompatibles
GET-Subset (`/discogs/releases|masters|artists|labels/{id}`) und den Lazy-Bilder-Cache.

> **Lesehinweis zu den Markierungen**
> - **[EMPIRISCH]** = selbst gemessen/abgerufen am 04.08.2026 gegen die echten
>   Endpunkte (Kommando + Ergebnis im Text). Diese Aussagen sind belastbar.
> - **[QUELLE]** = aus Dokumentation, Repo-Quelltext, Forum oder Websuche;
>   URL jeweils direkt dabei.
> - **[ANNAHME]** = Hochrechnung/Schätzung, als solche gekennzeichnet.

---

## 0. Kernaussagen auf einen Blick

1. **Die Dumps haben sich seit den Referenz-Tools massiv geändert.** Im
   August-2026-Dump gibt es **kein `<images>`-Element mehr** (nicht nur keine
   URLs — das Element fehlt komplett) und **kein `status`-Attribut** am
   `<release>`. Beides ist in `discogs-xml2db`/`discogs-load` fest verdrahtet.
   → Schema-Vorlagen müssen bereinigt werden. **[EMPIRISCH]**
2. **Der S3-Bucket ist dicht.** `discogs-data-dumps.s3.us-west-2.amazonaws.com`
   liefert für Listing *und* Einzelobjekt `403 AccessDenied`. Einziger Weg ist
   `https://data.discogs.com/`. **[EMPIRISCH]**
3. **`data.discogs.com` rate-limitet hart:** nach ~15 Anfragen in ~6 Minuten
   kam `429` mit `retry-after: 3574` (≈1 Stunde Sperre). Der tägliche
   Verfügbarkeits-Check muss deshalb **eine einzige** Anfrage pro Tag machen.
   **[EMPIRISCH]**
4. **`CHECKSUM.txt` ist der zuverlässige Fertig-Marker** — sie wird als
   *letzte* Datei geschrieben (in allen 8 Monaten 2026 nachweisbar). **[EMPIRISCH]**
5. **Kein Range/Resume:** `data.discogs.com` ignoriert `Range:` und liefert
   HTTP 200 mit der Volldatei. Ein Abbruch bei 10,4 GB bedeutet Neustart.
   **[EMPIRISCH]**
6. **CC0 gilt für alle vier Entitäten**, wortwörtlich auf der Downloadseite.
   Bilder sind davon *nicht* erfasst. **[EMPIRISCH/QUELLE]**
7. **Von den beiden HANDOFF-Referenztools taugt nur eines.** `discogs-load`
   (Rust, letzter Commit 2022) hat **keine** Tracklist, **keine**
   Release-Artists, **keine** Formats/Identifiers/Companies — als Vorlage
   unbrauchbar. `discogs-xml2db` **auf Branch `develop`** (nicht `master`!)
   ist der belastbare Standard. **[V]**
8. **Der Lazy-Bilder-Cache kollidiert mit den API-ToS** („may not cache or
   store the Content longer than is necessary", Bilder = *Restricted Data*,
   keine Weitergabe, keine kommerzielle Nutzung). Betreiber-Entscheidung
   nötig → **Ⓞ8**. **[QUELLE]**
9. **Der Dump-Umfang ist messbar:** 11,5 GiB gz → **≈ 70 GiB entpackt**,
   ~18 Mio. Releases, exakt **2.405.196 Labels**. **[EMPIRISCH]**

---

## 1. XML-Dump-Schema exakt

### 1.1 Dateien eines Monats-Dumps

Ein Monats-Dump besteht aus **5 Dateien** — vier gzip-komprimierte XML-Dateien
plus eine Prüfsummendatei: **[EMPIRISCH]**

```
discogs_YYYYMM01_artists.xml.gz
discogs_YYYYMM01_labels.xml.gz
discogs_YYYYMM01_masters.xml.gz
discogs_YYYYMM01_releases.xml.gz
discogs_YYYYMM01_CHECKSUM.txt
```

Es gibt **keine** weiteren Entitäten (keine Marketplace-, Nutzer-, Sammlungs-
oder Bilddaten).

### 1.2 Allgemeine Dateieigenschaften **[EMPIRISCH]**

Geprüft an Stichproben des Dumps **20260801** (labels vollständig, die anderen
drei als abgeschnittene Präfix-Stichprobe von je 6 MB gz):

| Eigenschaft | Befund |
|---|---|
| XML-Deklaration | **Fehlt.** Datei beginnt direkt mit `<labels>\n<label>…` bzw. `<releases>\n<release id="1">…`. Encoding ist implizit UTF-8. |
| Encoding | UTF-8, sauber dekodierbar; **keine** rohen Steuerzeichen < 0x20 außer TAB/LF |
| Zeilenumbrüche im Text | CR wird **immer** als `&#13;` maskiert (labels-Stichprobe: 192.046×), LF steht **roh** im Text |
| Record-Trennung | Jeder Top-Level-Record beginnt **am Zeilenanfang**; Textinhalte enthalten aber rohe LF → **zeilenweises Splitten ist falsch**, `^<release ` als Anker ist dagegen zuverlässig |
| Entities | nur `&amp;` `&lt;` `&gt;` `&quot;` + numerische Refs; keine benannten HTML-Entities |
| Wurzelelement | `<artists>`, `<labels>`, `<masters>`, `<releases>` — korrekt geschlossen (labels-Datei endet auf `</labels>`) |
| ID-Ort | **inkonsistent:** `artist`/`label` haben `<id>` als **Kindelement**, `master`/`release` haben `id` als **Attribut** |

### 1.3 Entität `label`

Vollständige Struktur (Stichprobe: 87.950 Records aus dem Dateianfang;
Zählungen = Vorkommen im Sample): **[EMPIRISCH]**

```
<label>                              (Top-Level, id als KINDELEMENT)
  <id>1</id>                         Pflicht  87950/87950
  <name>Planet E</name>              87949/87950  ← 1 Record OHNE name (id=212)
  <contactinfo>…</contactinfo>       31884   Freitext, mehrzeilig
  <profile>…</profile>               58748   Freitext mit Discogs-Markup [a=…], [l=…]
  <data_quality>Needs Vote</…>       87950   Enum-artig, aber Freitext
  <parentLabel id="4711">Name</…>    26575   Attribut @id + Textinhalt = Name
  <sublabels>                        13393
    <label id="31405">Name</label>   218678  @id + Text
  </sublabels>
  <urls>                             87950   Element immer da, oft als <urls/>
    <url>http://…</url>              57974
  </urls>
</label>
```

**Kein `<images>`.** In der **vollständigen** 422-MB-Datei: 0 Treffer für
`<images`. **[EMPIRISCH]**

Gesamtzahlen aus der vollständigen labels-Datei: **2.405.196 Labels**,
höchste ID **4.655.154**, **keine** doppelten IDs, **1** Label ohne `<name>`.
**[EMPIRISCH]**

### 1.4 Entität `artist`

Stichprobe: 30.762 Records. **[EMPIRISCH]**

```
<artist>                             (id als KINDELEMENT)
  <id>1</id>                         30762
  <name>The Persuader</name>         30762  (im Sample lückenlos)
  <realname>Jesper Dahlbäck</…>      13948
  <profile>…</profile>               14543  Freitext + Discogs-Markup
  <data_quality>…</data_quality>     30762
  <urls><url>…</url></urls>          11726 / 38830
  <namevariations>                   18346
    <name>Persuader</name>           105558  ← OHNE id (reiner String)
  </namevariations>
  <aliases>                          17006
    <name id="239">Jesper Dahlbäck</name>   94617  ← MIT @id
  </aliases>
  <groups>                           4088
    <name id="34803">E-Culture</name>       20288  ← MIT @id
  </groups>
  <members>                          11762
    <name id="26">Alexi Delano</name>       37338  ← MIT @id
  </members>
</artist>
```

Fallstrick: `namevariations/name` hat **kein** `@id`, `aliases|groups|members/name`
haben eines. Drei strukturell gleiche, semantisch verschiedene Listen.
**Kein `<images>`.** **[EMPIRISCH]**

### 1.5 Entität `master`

Stichprobe: 13.214 Records. **[EMPIRISCH]**

```
<master id="113">                    ← id als ATTRIBUT
  <main_release>116925</main_release>   13214 (lückenlos)
  <artists>                             13214
    <artist>                            14803
      <id>3225</id> <name>…</name>
      <anv>Mix Race</anv>               1395   (Artist Name Variation)
      <join>,</join>                    13063  (Verknüpfungszeichen)
    </artist>
  </artists>
  <genres><genre>Electronic</genre></genres>    13214 / 15830
  <styles><style>Techno</style></styles>        13098 / 27367
  <year>2002</year>                     13214  (36× Wert "0" = unbekannt)
  <title>Moments In Time</title>        13214
  <data_quality>…</data_quality>        13214
  <notes>…</notes>                      1645
  <videos>                              12449
    <video src="https://www.youtube.com/watch?v=…" duration="321" embed="true">
      <title>…</title>                  79542/79551  ← 9 Videos OHNE title
      <description>…</description>      73321/79551
    </video>
  </videos>
</master>
```

Der Master hat **keine** Tracklist, **keine** `extraartists`, **kein**
`<images>`, **kein** `role`. **[EMPIRISCH]**

### 1.6 Entität `release`

Stichprobe: 9.163 Records (ids 1…~9.200). **[EMPIRISCH]**

```
<release id="1">                     ← id als ATTRIBUT; KEIN status-Attribut mehr!
  <artists>                             9163
    <artist>                            10022
      <id>1</id><name>The Persuader</name>
      <anv>Casy Hogan</anv>             1010
      <join>&amp;</join>                859
    </artist>
  </artists>
  <title>Stockholm</title>              9163
  <labels>                              9163
    <label name="Svek" catno="SK032" id="5"/>   10720  ← reine Attribute
  </labels>
  <extraartists>                        7293
    <artist>                            25649
      <id>507025</id>                   25219   ← 430 OHNE <id> (1,7 %!)
      <name>…</name>                    25649
      <anv>…</anv>                      8519
      <role>Lacquer Cut By</role>       25649
      <tracks>9, 10</tracks>            2151    ← Positions-Liste als Freitext
    </artist>
  </extraartists>
  <formats>                             9163
    <format name="Vinyl" qty="2" text="">   9239  (@text: 8136 leer, 644 fehlt, 459 gefüllt)
      <descriptions><description>12"</description></descriptions>  9070 / 17761
    </format>
  </formats>
  <genres><genre>Electronic</genre></genres>   9163 / 9643
  <styles><style>Deep House</style></styles>   9155 / 18013
  <country>Sweden</country>             9159   ← 4 ohne country
  <released>1999-03-00</released>       9011   ← siehe Datumsformate unten
  <notes>…</notes>                      6788   (bis 4.789 Zeichen im Sample)
  <data_quality>Correct</data_quality>  9163
  <master_id is_main_release="true">1660109</master_id>   9163
                                        ← Element IMMER da; Wert "0" = kein Master (1575×)
  <tracklist>                           9163
    <track>                             53633
      <position>A</position>            52236   ← 1397 Tracks OHNE position
      <title>Östermalm</title>          53633   (lückenlos)
      <duration>4:45</duration>         36129
      <artists><artist>…</artist></artists>          14542 / 15441
      <extraartists><artist>…<role>Remix</role></artist></extraartists>  15898 / 31673
      <sub_tracks>                      23      ← geschachtelte Tracks!
        <track><position>11.a</position><title>909 Shuffle</title>
               <duration>3:10</duration>
               <artists>…</artists><extraartists>…</extraartists></track>
      </sub_tracks>
    </track>
  </tracklist>
  <identifiers>                         7124
    <identifier type="Matrix / Runout" description="A-side runout" value="…"/>   29637
                                        ← @description nur 29471× vorhanden
  </identifiers>
  <companies>                           6749
    <company>                           33322
      <id>271046</id>                   33318  ← 4 OHNE id
      <name>The Globe Studios</name>    33322
      <entity_type>23</entity_type>     33322
      <entity_type_name>Recorded At</…> 33322
      <catno>X9694</catno>              1772
    </company>
  </companies>
  <series>                              582
    <series name="Profound Sounds" catno="Vol. 1" id="527772"/>   591
  </series>
  <videos><video src="…" duration="325" embed="true">
     <title>…</title><description>…</description></video></videos>   7442 / 31454
</release>
```

**Kein `<images>`, kein `status`.** **[EMPIRISCH]**

#### Wichtige Werteräume (aus der Stichprobe) **[EMPIRISCH]**

- **`<released>`**: `YYYY` (3.849), `YYYY-MM-DD` vollständig (2.912),
  `YYYY-MM-00` (1.537), `YYYY-00-00` (713), fehlt/leer (152).
  → **Nicht** als SQL-`date` speichern; `text` + zusätzlich normalisierte
  Spalten `released_year/month/day` (smallint, 0 = unbekannt).
- **`identifier@type`** (Top): `Matrix / Runout` 13.150, `Barcode` 6.683,
  `Mould SID Code` 2.923, `Rights Society` 2.560, `Mastering SID Code` 2.168,
  `Label Code` 1.271, `Other` 354, `Price Code` 326, `Distribution Code` 64,
  `ISRC` 62, `Pressing Plant ID` 37, `SPARS Code` 32. Freitext, nicht fix.
- **`company/entity_type` → `entity_type_name`** (Top): 21→`Published By`,
  14→`Copyright (c)`, 13→`Phonographic Copyright (p)`, 9→`Distributed By`,
  17→`Pressed By`, 30→`Lacquer Cut At`, 29→`Mastered At`, 23→`Recorded At`,
  10→`Manufactured By`, 31→`Glass Mastered At`, 6→`Licensed From`,
  37→`Produced For`, 27→`Mixed At`, 26→`Produced At`. Beide Felder sind
  redundant mitgeliefert; der Spiegel braucht keine eigene Lookup-Tabelle.
- **`genre`**: 12 verschiedene Werte im Sample (kontrolliertes Vokabular:
  Classical, Electronic, Folk/World/&/Country, Funk/Soul, Hip Hop, Jazz,
  Latin, Non-Music, Pop, Reggae, Rock, Stage & Screen). `style` ist viel
  größer und faktisch offen.
- **Maximale Feldlängen im Sample** (Dimensionierung, keine harte Obergrenze!):
  `title` 105, `notes` 4.789, `country` 31, `artist/name` 74, `anv` 65,
  `role` **320**, `join` 19, `tracks` 62, `identifier@value` **1.351**,
  `identifier@description` 99, `track/position` 22, `track/title` 132,
  `track/duration` 7. → Alle Textspalten als `text`, nicht `varchar(n)`.
- **`role`**: 47.019 einfache Rollen, 10.328 mit `[Detail]`-Klammerzusatz
  (z. B. `Written-By [All Tracks By]`), 14.738 mit Komma
  (Mehrfachrollen in einem Feld). → **Roh speichern**, Parsing optional
  in einer View. Beide Referenz-Tools speichern ebenfalls roh.

#### Tracklist-Semantik: der `type_`-Fallstrick **[EMPIRISCH]**

Der Dump kennt **kein** `type_`-Feld — die API liefert es
(`track` / `heading` / `index`). Es muss aus der Struktur abgeleitet werden:

| Dump-Struktur | API-`type_` |
|---|---|
| Track hat `<sub_tracks>` | `index` |
| Track ohne `<position>` **und** ohne `<duration>` | `heading` |
| sonst | `track` |

Belegte Beispiele: Release 723 hat einen Track *Dreammaker* (13:04) mit drei
`sub_tracks` `11.a`–`11.c`; Release 25/26 haben Tracks `<track><title>This
Side</title></track>` ohne Position und Dauer (= Überschriften).

### 1.7 Änderungen gegenüber dem, was die Referenz-Tools erwarten

Gegenprobe an historischen Dumps derselben Quelle (jeweils 1–3 MB gz Präfix
der `releases`-Datei): **[EMPIRISCH]**

| Dump | `status`-Attribut | `<images>`-Element |
|---|---|---|
| 2020-01-01 | **ja** (`<release id="1" status="Accepted">`) | **ja** — `<image height="600" type="primary" uri="" uri150="" width="600"/>` (URIs leer) |
| 2022-01-01 | ja | ja (Attributreihenfolge geändert: `type,uri,uri150,width,height`) |
| 2024-01-01 | ja | ja |
| 2025-01-01 | **ja** | **nein** (0 Treffer) |
| 2026-08-01 | **nein** (0 Treffer) | **nein** (0 Treffer) |

→ Das `<images>`-Element ist zwischen **2024-01 und 2025-01** ganz entfallen
(vorher: vorhanden, aber `uri`/`uri150` seit Jahren leer). Das
`status`-Attribut ist zwischen **2025-01 und 2026-08** entfallen. Die exakten
Übergangsmonate konnten wegen der 1-Stunden-Sperre (429, s. §2.5) nicht weiter
eingegrenzt werden — **offener Punkt Ⓞ1**.

Hintergrund zur Bild-Entfernung: Discogs hat die Bild-Metadaten zur
Größenreduktion aus dem Dump genommen und angekündigt, sie draußen zu lassen,
falls es keine Beschwerden gibt.
**[QUELLE]** https://www.discogs.com/forum/thread/411182 ·
https://www.discogs.com/forum/thread/756269

**Konsequenz für M3:** Jede Schema-Vorlage aus `discogs-xml2db` /
`discogs-load` enthält `release.status` und eine `image`-Tabelle. Beide
laufen im aktuellen Dump leer bzw. brechen bei `NOT NULL`. Das Schema muss
davon befreit werden (siehe §1.9).

### 1.8 Referenz-Tooling (Schema-Vorlagen)

Alle Aussagen in diesem Abschnitt sind, wo mit **[V]** markiert, im
**Quelltext/DDL der Repos verifiziert**; **[R]** = nur aus README/Issue.

#### Repo-Identität — Vorsicht bei den Links aus HANDOFF §14.2

- **`discogs-load`** ist das Rust-Projekt von **DylanBartels**:
  https://github.com/DylanBartels/discogs-load — **letzter Commit 2022-03-25**,
  also seit ~4,5 Jahren tot; zwei offene Issues unbeantwortet. **[V]**
- **`discogs-xml2db`** von **philipmat**:
  https://github.com/philipmat/discogs-xml2db — **Default-Branch ist
  `develop`, nicht `master`.** `master` ist der tote v1-Stand (letzter Commit
  2020-08-04), `develop` ist aktiv (letzter Commit **2026-02-08**).
  **Wer `master` liest, liest das falsche Projekt.** **[V]**

#### `discogs-load` — **als Vorlage unbrauchbar**

7 Tabellen (`artist`, `label`, `master`, `master_artist`, `release`,
`release_label`, `release_video`), array-lastig (`text[]` statt Kindtabellen).
Es fehlen **strukturell**: Release-Artists, `extraartists`, **Tracklist**,
Track-Artists, Formats, Identifiers, Companies, Images. **[V]**
`sql/tables/release.sql` deckt nur
`id, status, title, country, released, notes, genres[], styles[], master_id,
data_quality` ab.

Dazu vier im Quelltext verifizierte **Bugs**, die weitere Spalten still leer
lassen: **[V]**
- `artist.name_variations`/`aliases`/`members` bleiben **immer leer** (kein
  `match`-Arm für `NameVariations`; die Arme für `aliases`/`members` suchen
  `<alias>`/`<member>`, der Dump liefert aber `<name id=…>`).
- `master.year/notes/genres/styles` werden nie befüllt.
- `release_label` verliert massiv Zeilen (HashMap auf `label_id` statt
  `(release_id, label_id)` → pro Batch nur die erste Zuordnung je Label).
- `release_video.title` ist immer `''`.

Ladeweg: `COPY … FROM STDIN **BINARY**` via `binary_copy` — technisch das
Schnellste. Laufzeit „~15 Minuten für die 10-GB-Releases-Datei auf einem
M1 Air" **[R]**, aber für das Rumpf-Schema und deshalb **nicht** mit den
anderen Zahlen vergleichbar. Keine einzige FK-Constraint im Projekt. **[V]**

→ **Nicht als Vorlage verwenden.** Es fehlt genau das, was dieses Projekt
braucht (Tracks, Positionen, Dauern, Barcodes, Artist-Rollen).

#### `discogs-xml2db` (`develop`) — **der belastbare Referenz-Standard**

**27 Tabellen**, durchgängig normalisiert. DDL:
https://github.com/philipmat/discogs-xml2db/blob/develop/postgresql/sql/CreateTables.sql **[V]**

```
artist, artist_url, artist_namevariation, artist_alias, artist_image, group_member
label, label_url, label_image
master, master_artist, master_video, master_genre, master_style, master_image
release, release_artist, release_label, release_genre, release_style,
release_format, release_track, release_track_artist, release_identifier,
release_video, release_company, release_image
```

Bemerkenswerte Designentscheidungen: **[V]**
- **`release_artist` mischt `artists` und `extraartists`** in eine Tabelle,
  unterschieden durch `extra` (in `release_artist` `integer`, in
  `release_track_artist` `boolean` — Inkonsistenz).
- **Tracklisten werden flachgeklopft:** laufender `track_id` + je Release
  zurückgesetzte `sequence`; `<sub_tracks>` werden rekursiv aufgelöst und über
  `parent` = `sequence` des Elterntracks verknüpft. **Es gibt keine echten
  Discogs-Track-IDs** — der Dump liefert keine (deckt sich mit unserem
  Schema-Vorschlag §1.9).
- `release_format.descriptions` ist **ein Textfeld** mit `"; "`-Verkettung,
  keine Kindtabelle.
- `release_format.qty` ist **`NUMERIC`** — mit DDL-Kommentar: Release 8262262
  hat eine 64-stellige `qty`. **Wichtige Warnung für unser `integer`!**
- **`*_image` hat keine `uri`-Spalte**, nur `type/width/height` — bewusst, weil
  die URIs im Dump seit Jahren leer waren.
- Import-Reihenfolge explizit dokumentiert: `CreateTables.sql` →
  `importcsv.py` → `CreatePrimaryKeys.sql` → `CreateFKConstraints.sql` →
  `CreateIndexes.sql`. **Erst Daten, dann PKs/FKs/Indizes.** 20 PKs,
  25 FK-Constraints, 32 Indizes.
- Zwischenformat ist **CSV** (optional bz2), Import per
  `COPY … FROM STDIN WITH CSV HEADER` (`psycopg2.copy_expert`).
- Parser: `lxml.iterparse` mit Element-Aufräumen — echtes Streaming.

**Reaktion auf die kaputten Dumps von 2025** (die 6 Zeilen Unterschied
`master` → `develop`): `artist.name` und `artist_url.url` verlieren
`NOT NULL`; `release_track.parent`, `release_track.track_id`,
`release_track_artist.track_id/track_sequence` wechseln von `integer` auf
**`text`** (weil Track-Positionen wie `"A1"` keine Zahlen sind). **[V]**
→ Genau die Fallstricke, die wir in §1.10 unabhängig gemessen haben.

**Laufzeiten** (nur XML→CSV, Dump 20200806, Hardware nicht genannt) **[R]**:

| Datei | Records | Python | C# |
|---|---:|:---:|:---:|
| artists | 7.046.615 | 6:22 | 2:35 |
| labels | 1.571.873 | 1:15 | 0:22 |
| masters | 1.734.371 | 3:56 | 1:57 |
| releases | 12.867.980 | **1:45:16** | **42:38** |

Der COPY-Import kommt obendrauf. Auf **Array-Spindeln** (HANDOFF §2) ist mit
einem Vielfachen zu rechnen — der Erst-Import gehört klar in die
Bootstrap-Zeitplanung (HANDOFF §15.2 Risiko 1).

**Größenordnung des Zielbestands** (Dump 20241201, CSV-Zeilen aus Issue #155)
**[R]**: rund **700 Mio. Zeilen** gesamt, davon `release_track` **164,6 Mio.**,
`release_track_artist` **116,0 Mio.**, `release_artist` **86,4 Mio.**,
`release_video` **63,8 Mio.**, `release` **47,1 Mio.**(?).
→ Das stützt die HANDOFF-§15.1-Schätzung von **100–180 GB** für die
Discogs-Postgres inkl. Indizes; bei 700 Mio. Zeilen ist eher das **obere**
Ende realistisch.

#### Unabhängige Bestätigung unserer Messungen durch die Issue-Historie

Die in §1.10 gemessenen Fallstricke sind im Referenz-Repo als offene Issues
dokumentiert — das ist eine starke Kreuzvalidierung: **[R]**

| Unser Befund (§1.10) | Issue |
|---|---|
| fehlende `<id>` bei Credits/Companies | #151 (`master_artist.artist_id` null), #152 (`release_artist.artist_id`), #153 (`release_company.company_id`), #154 (`release_track_artist.artist_id`) — **alle vier offen**, Dumps 20241201/20250101 |
| Label 212 ohne `<name>` | #148, #158 (Fix in `exporter.py` auf `develop`) |
| Track-Positionen sind keine Zahlen (`"A1"`) | #131 → Typwechsel `integer`→`text` |
| `<images>` verschwunden | **#155: seit Dump 20241201** enthalten `artist_image.csv`, `label_image.csv`, `master_image.csv`, `release_image.csv` **nur noch die Kopfzeile** (vorher 2,5 / 0,57 / 8,0 / **51,8 Mio.** Zeilen) |
| unbekannte Artists ohne `id` (vorher `0`) | ebenfalls #155 |

→ **Ⓞ1 ist damit gelöst:** Das `<images>`-Element ist mit dem Dump
**20241201** entfallen. Das passt exakt zu unserer Messung (2024-01 noch da,
2025-01 schon weg).

Weitere dokumentierte Fallstricke: **[R]**
- **FK-Constraints laufen auf echten Dumps nicht sauber durch** (#125, offen
  seit 2020): `release_artist.artist_id = 0` ohne Gegenstück in `artist`,
  `release_company.company_id` ohne Gegenstück in `label`.
  → **Für uns: FKs nur dort setzen, wo sie halten; Credit-FKs weglassen
  oder `NOT VALID`.**
- „Ungültige XML-Zeichen" und `fix-xml.py` sind ein **v1-Problem**; in v2
  gibt es kein `fix-xml.py` mehr, `lxml` verkraftet die Dumps direkt. **[V]**
  Deckt sich mit unserer Messung (keine rohen Steuerzeichen, §1.10 Punkt 19).
- Ein Artist heißt buchstäblich `" "` (non-breaking space) und crasht
  den Exporter (#156).
- `sha256sum -c discogs_*_CHECKSUM.txt` wird im README empfohlen — funktioniert
  aber nur nach Format-Normalisierung (unser Befund §2.4).

#### Alternativen (Kurzbewertung)

| Tool | Sprache | Letzter Commit | Postgres | Schema | Urteil |
|---|---|---|---|---|---|
| **discogskit** ([jmfontaine](https://github.com/jmfontaine/discogskit)) | Python | **2026-04-02** | **direkt (ADBC/COPY)** | voll, 21 Tab. (ohne images) | **schnellste Option**, aber v0.1.0 |
| **discogs-xml2db** (`develop`) | Python/C# | 2026-02-08 | via CSV | voll, 27 Tab. | **sicherste Vorlage** |
| [dgtools](https://github.com/marcw/dgtools) | Go | 2025-09-14 | direkt | **hybrid**: Tracks/Formats/IDs als `jsonb` | eingeschränkt |
| [disco-quick](https://github.com/sublipri/disco-quick) | Rust | 2025-05-21 | — (Bibliothek) | voll (nur Structs) | bestes Parser-Fundament; **5,2 MB Peak-RAM** für einen Releases-Durchlauf |
| [discogs2pg](https://github.com/clrnd/discogs2pg) | Haskell | 2018-05-03 | direkt | voll | tot (Vorlage für discogs-load) |
| [discogs-load](https://github.com/DylanBartels/discogs-load) | Rust | 2022-03-25 | direkt | **7 Tab., lückenhaft** | **nein** |
| [andrewh/discogs-load](https://github.com/andrewh/discogs-load) | Rust | 2026-05-26 | direkt | unverändert lückenhaft | nein |
| [discogs-data-tools](https://github.com/flut1/discogs-data-tools) | JS | 2019-01-26 | nein (MongoDB) | — | nein |
| [elpassion/discogs_data](https://github.com/elpassion/discogs_data) | Ruby | 2020-10-06 | kein Writer | — | nein |
| [discogsography](https://github.com/SimplicityGuy/discogsography) | Python | 2026-08-04 | ja (+Neo4j) | — | ganze Plattform, zu schwer |

**`discogskit`** ist der bemerkenswerteste Fund: volles Schema **und**
direkter COPY-Pfad (Arrow → ADBC `adbc_ingest`), plus `--pg-unlogged`,
parallele Index-Erstellung und temporäres `max_wal_size`-Tuning **[V]**.
README-Benchmark (Dump 20260301, PostgreSQL 18, MacBook Air M3 24 GB) **[R]**:
`discogs-xml2db` Python **1:42:35** · `discogskit` **33:49** (3,0×) ·
`discogskit --pg-unlogged` **11:22** (**9,0×**).
Vorbehalt: v0.1.0, ein Maintainer, Ende 2025 entstanden.

**DuckDB** ist kein eigenes Tool, sondern ein nachgelagerter Schritt; der
meistzitierte Blogpost nutzt `discogs-xml2db` für XML→CSV und lädt dann per
`read_csv`. Für ein Postgres-Ziel bringt das nichts. **[QUELLE]**
https://www.architecture-performance.fr/ap_blog/trying-duckdb-with-discogs-data/

**Konsequenz für M3:** Das Projekt schreibt seinen Importer ohnehin selbst
(Ein-Container-Modell, Etappen, Resume, `dump_state` — HANDOFF §3/§8/§11.3).
`discogs-xml2db/develop` dient als **Schema- und Fallstrick-Vorlage**, nicht
als Abhängigkeit. Siehe Ⓞ13.

### 1.9 Empfohlenes Zielschema `discogs` (Ableitung)

Vorschlag als Konkretisierung von HANDOFF §8 („Struktur gemäß XML-Dump;
etabliertes Schema-Tooling als Vorlage"). Bewusst **an der Dump-Struktur
orientiert, nicht an der API** — der API-Nachbau (§5) ist eine reine
Lese-Projektion darüber.

```sql
CREATE SCHEMA discogs;

-- ── Stammdaten ───────────────────────────────────────────────────────────
CREATE TABLE discogs.artist (
  id            integer PRIMARY KEY,
  name          text,                    -- nullable! (siehe Fallstrick 10)
  real_name     text,
  profile       text,
  data_quality  text
);
CREATE TABLE discogs.artist_url        (artist_id integer, seq smallint, url text);
CREATE TABLE discogs.artist_namevariation (artist_id integer, seq smallint, name text);
CREATE TABLE discogs.artist_alias      (artist_id integer, alias_id integer, name text);
CREATE TABLE discogs.artist_group      (artist_id integer, group_id integer, name text);
CREATE TABLE discogs.artist_member     (artist_id integer, member_id integer, name text);

CREATE TABLE discogs.label (
  id            integer PRIMARY KEY,
  name          text,                    -- nullable! (id 212 hat keinen)
  contact_info  text,
  profile       text,
  data_quality  text,
  parent_id     integer,                 -- aus <parentLabel @id>
  parent_name   text                     -- Text von <parentLabel>
);
CREATE TABLE discogs.label_url         (label_id integer, seq smallint, url text);
CREATE TABLE discogs.label_sublabel    (label_id integer, sublabel_id integer, name text);

CREATE TABLE discogs.master (
  id            integer PRIMARY KEY,     -- aus @id
  main_release  integer,
  title         text,
  year          smallint,                -- 0 = unbekannt → NULL
  notes         text,
  data_quality  text
);
CREATE TABLE discogs.master_artist  (master_id integer, seq smallint,
                                     artist_id integer, name text, anv text, join_str text);
CREATE TABLE discogs.master_genre   (master_id integer, seq smallint, genre text);
CREATE TABLE discogs.master_style   (master_id integer, seq smallint, style text);
CREATE TABLE discogs.master_video   (master_id integer, seq smallint,
                                     src text, duration integer, embed boolean,
                                     title text, description text);

CREATE TABLE discogs.release (
  id             integer PRIMARY KEY,    -- aus @id
  title          text,
  country        text,
  released       text,                   -- ROH, z. B. '1999-03-00'
  released_year  smallint,               -- abgeleitet, 0/NULL = unbekannt
  released_month smallint,
  released_day   smallint,
  notes          text,
  data_quality   text,
  master_id      integer,                -- 0 → NULL
  is_main_release boolean                -- aus <master_id @is_main_release>
  -- KEIN status: existiert im Dump 2026 nicht mehr
);
CREATE TABLE discogs.release_artist      (release_id integer, seq smallint,
                                          artist_id integer, name text, anv text, join_str text);
CREATE TABLE discogs.release_credit (    -- <extraartists> auf Release-Ebene
  release_id integer, seq smallint,
  artist_id  integer,                    -- NULLABLE (1,7 % fehlen)
  name text, anv text, role text, tracks text
);
CREATE TABLE discogs.release_label       (release_id integer, seq smallint,
                                          label_id integer, label_name text, catno text);
CREATE TABLE discogs.release_format      (release_id integer, seq smallint,
                                          name text, qty numeric, text_note text);
        -- qty bewusst NUMERIC, nicht integer: Release 8262262 hat laut
        -- discogs-xml2db-DDL eine 64-stellige qty (§1.8)
CREATE TABLE discogs.release_format_description (release_id integer, format_seq smallint,
                                          seq smallint, description text);
CREATE TABLE discogs.release_genre       (release_id integer, seq smallint, genre text);
CREATE TABLE discogs.release_style       (release_id integer, seq smallint, style text);
CREATE TABLE discogs.release_identifier  (release_id integer, seq smallint,
                                          type text, value text, description text);
CREATE TABLE discogs.release_company     (release_id integer, seq smallint,
                                          company_id integer,     -- NULLABLE
                                          name text, entity_type text,
                                          entity_type_name text, catno text);
CREATE TABLE discogs.release_series      (release_id integer, seq smallint,
                                          series_id integer, name text, catno text);
CREATE TABLE discogs.release_video       (release_id integer, seq smallint,
                                          src text, duration integer, embed boolean,
                                          title text, description text);

-- ── Tracklist inkl. sub_tracks ───────────────────────────────────────────
CREATE TABLE discogs.track (
  id            bigserial PRIMARY KEY,   -- Dump hat KEINE Track-ID
  release_id    integer NOT NULL,
  parent_id     bigint,                  -- NULL = Top-Level; sonst Index-Track
  seq           integer NOT NULL,        -- Dokumentreihenfolge
  position      text,                    -- kann leer sein
  title         text NOT NULL,
  duration      text,                    -- 'M:SS', NICHT interval
  type_         text NOT NULL            -- abgeleitet: track|heading|index
);
CREATE TABLE discogs.track_artist (track_id bigint, seq smallint,
                                   artist_id integer, name text, anv text, join_str text);
CREATE TABLE discogs.track_credit (track_id bigint, seq smallint,
                                   artist_id integer,   -- NULLABLE
                                   name text, anv text, role text);

-- ── Betrieb (HANDOFF §8) ─────────────────────────────────────────────────
CREATE TABLE discogs.dump_state (
  dump_id      text PRIMARY KEY,         -- '20260801'
  entity       text NOT NULL,            -- artists|labels|masters|releases
  sha256       text,
  bytes        bigint,
  etag         text,
  downloaded_at timestamptz,
  imported_at  timestamptz,
  rows_in      bigint,
  status       text                      -- pending|downloading|importing|done|failed
);
```

**Designentscheidungen und ihre Begründung:**

- **Kein `image`-Table**, keine `release.status` — der Dump liefert beides
  nicht mehr (§1.7). Bilder sind ein eigenständiges Subsystem (§4) und
  gehören nach HANDOFF §8 ohnehin ins `covers`-Schema.
- **Alle Textspalten `text`**, keine `varchar(n)`: gemessene Maxima
  (`role` 320, `identifier@value` 1.351) sind keine Obergrenzen, und
  Postgres hat bei `text` keinen Nachteil.
- **`seq`-Spalten überall**: Der Dump kodiert Reihenfolge nur über die
  Dokumentposition. Ohne `seq` lässt sich die API-Antwort (§5) nicht
  reihenfolgetreu rekonstruieren — das ist der häufigste Nachbaufehler.
- **`name` wird neben der ID mitgespeichert** (bei `release_artist`,
  `release_label`, `label_sublabel` usw.), obwohl das denormalisiert ist:
  Der Dump liefert den zum Dump-Zeitpunkt gültigen Namen, und die API gibt
  genau diesen zurück. Ein Join auf `artist.name` würde bei fehlender ID
  (1,7 %) außerdem Werte verlieren.
- **`track.id` ist synthetisch** — der Dump vergibt keine Track-IDs.
  Konsequenz: Ein Voll-Reimport ändert alle Track-IDs. Deshalb dürfen
  Track-IDs **niemals** nach außen (API/`mbref`) exponiert werden.
- **FKs erst nach dem Import anlegen**, `COPY` in leere Tabellen ohne
  Indizes, danach Indizes + Constraints — anders ist die Größenordnung
  (~17 Mio. Releases, >100 Mio. Tracks/Credits) auf Array-Spindeln nicht
  in vertretbarer Zeit zu laden.
- **Import-Reihenfolge:** `artists` → `labels` → `masters` → `releases`
  (klein nach groß, Referenzen zuerst). Passt zur Etappen-Anforderung aus
  HANDOFF §11.3 („pro Entitätstyp/Datei eine Etappe").

### 1.10 Bekannte Fallstricke — Checkliste für den Importer

Alle Punkte mit **[E]** sind in dieser Recherche selbst reproduziert worden.

**Struktur / Schema**

1. **[E] `<images>` gibt es nicht mehr.** Weder Element noch URIs. Keine
   `image`-Tabelle anlegen; Bilder ausschließlich über die API (§4).
2. **[E] `status` am `<release>` gibt es nicht mehr.** Spalte weglassen oder
   nullable halten. Wer aus alten Dumps migriert, muss beides tolerieren.
3. **[E] ID-Position ist uneinheitlich:** `artist`/`label` → `<id>`-Kindelement,
   `master`/`release` → `id`-Attribut. Ein generischer Parser braucht beide Pfade.
4. **[E] `master_id` ist immer da, `0` bedeutet „kein Master"** (1.575 von
   9.163 im Sample). Wer `0` als FK auf `master` schreibt, erzeugt
   FK-Verletzungen. → beim Import auf `NULL` mappen.
5. **[E] `<sub_tracks>` verschachtelt `<track>` in `<track>`** — inklusive
   eigener `artists`/`extraartists`. Eine flache Track-Tabelle braucht
   `parent_track_id` + `sequence`, sonst gehen 133 von 53.766 Tracks im
   Sample verloren (bzw. werden dem falschen Release zugeordnet).
6. **[E] `type_` (`track`/`heading`/`index`) fehlt im Dump** und muss abgeleitet
   werden (Regel in §1.6). Ohne das kann der API-Nachbau die Tracklist nicht
   korrekt ausliefern.
7. **[E] Drei verschiedene `<name>`-Listen beim Artist:** `namevariations`
   (ohne `@id`), `aliases`/`groups`/`members` (mit `@id`). Nicht in eine
   Tabelle werfen.
8. **[E] `parentLabel` trägt den Namen als Text und die ID als Attribut** —
   dasselbe Muster wie `sublabels/label`. Redundant zur `label`-Tabelle, aber
   nützlich für den API-Nachbau ohne Join.

**Fehlende / kaputte Werte**

9. **[E] Fehlende IDs bei Credits:** 430 von 25.649 `extraartists/artist`
   (1,7 %) haben **kein** `<id>`; ebenso 4 von 33.322 `companies/company`.
   → Credit-Tabelle darf `artist_id` **nicht** `NOT NULL` haben, und ein
   FK auf `artist` muss nullable sein.
10. **[E] Ein Label ohne `<name>`** (id 212, Profil „[b]DO NOT USE.[/b]") in
    2.405.196 Labels. → `name` nullable oder auf `''` defaulten; ein
    `NOT NULL` bricht den kompletten `COPY`-Batch.
11. **[E] 9 von 79.551 `master/videos/video` ohne `<title>`.**
12. **[E] 1.397 von 53.633 Tracks ohne `<position>`** (Überschriften/Index).
    `position` ist **kein** Schlüssel und **nicht** sortierbar
    (`A`, `B1`, `11.a`, `CD1-3`, bis 22 Zeichen). → eigene `sequence`-Spalte
    aus der Dokumentreihenfolge vergeben.
13. **[E] 4 von 9.163 Releases ohne `<country>`**, 152 ohne `<released>`.
14. **[E] `format@text` ist in 8.136 von 9.239 Fällen ein *leerer String*,
    in 644 Fällen fehlt das Attribut ganz** — `''` und `NULL` sind hier
    unterschiedliche Zustände; einheitlich auf `NULL` normalisieren.
15. **[E] `<released>` ist kein Datum:** `YYYY`, `YYYY-00-00`, `YYYY-MM-00`
    oder vollständig. `1999-03-00` sprengt jeden `::date`-Cast.
    → als `text` speichern + abgeleitete `year/month/day`-smallints.
16. **[E] `master/year` kann `0` sein** (36 von 13.214).

**Encoding / Parsing**

17. **[E] Keine XML-Deklaration.** Parser, die `encoding=` erwarten, müssen
    UTF-8 annehmen. (`ET.fromstring` auf Bytes funktioniert.)
18. **[E] CR ist als `&#13;` maskiert, LF steht roh im Text.** Ein
    zeilenbasierter Splitter zerlegt Records mitten im `<profile>`/`<notes>`.
    Zuverlässig ist dagegen: **jeder Top-Level-Record beginnt am Zeilenanfang**
    (`(?m)^<release `) — das erlaubt Chunking für parallele Verarbeitung ohne
    vollen XML-Parse.
19. **[E] Keine rohen Steuerzeichen < 0x20** außer TAB/LF in den geprüften
    Stichproben — d. h. die historischen „invalid XML character"-Probleme
    älterer Dumps treten im aktuellen Dump **nicht** mehr auf. Trotzdem
    defensiv parsen (der Dump wird maschinell aus der Live-DB erzeugt).
20. **[E] Discogs-Markup in Freitexten:** `<profile>`/`<notes>` enthalten
    `[a=Carl Craig]`, `[l=Seasons Recordings]`, `[b]…[/b]`, `[url=…]`.
    Roh speichern — die offizielle API liefert es genauso zurück.
21. **Größe:** Der `releases`-Stream ist ~65 GiB entpackt (§2.6). **Nie**
    vollständig in den Speicher parsen (`ET.fromstring`/`minidom`), sondern
    `iterparse` + `elem.clear()` bzw. SAX; Postgres über `COPY`, nicht
    `INSERT`. Für den Ein-Container-Betrieb (HANDOFF §3) heißt das:
    Import in Etappen pro Entitätsdatei, Fortschritt in `dump_state`,
    resumierbar (HANDOFF §8/§11.3).
22. **[E] Der Download selbst ist der fragilste Schritt** — kein Resume,
    Stundensperre bei 429 (§2.5).

**Prozess**

23. **[E] Dumps können verspätet (Januar 2026: erst am 15.) oder nachträglich
    ersetzt (August 2026: `artists` am Folgetag) erscheinen.** Der Check muss
    täglich laufen und ETag/Prüfsumme vergleichen, nicht nur die Existenz.
24. **[E] `CHECKSUM.txt` ist der Fertig-Marker** — vor ihrem Erscheinen
    dürfen die `.xml.gz` als unvollständig gelten.
25. **[E] `CHECKSUM.txt` ist nicht `sha256sum -c`-kompatibel** (ein statt
    zwei Leerzeichen).

---

## 2. Veröffentlichung: URLs, Rhythmus, Listing, Checksummen, Größen

### 2.1 URL-Struktur

**Offizieller Einstieg:** `https://data.discogs.com/` — HTML-Listing, das die
S3-Objekte des Buckets `discogs-data-dumps` (Region `us-west-2`, Prefix
`data/`) spiegelt. **[EMPIRISCH]**

```
Jahresliste:   https://data.discogs.com/?prefix=data%2F2026%2F
Einzeldatei:   https://data.discogs.com/?download=data%2F2026%2Fdiscogs_20260801_releases.xml.gz
Prüfsummen:    https://data.discogs.com/?download=data%2F2026%2Fdiscogs_20260801_CHECKSUM.txt
```

Der Dateiname ist **vollständig deterministisch**:
`data/{YYYY}/discogs_{YYYY}{MM}01_{artists|labels|masters|releases}.xml.gz`
— der Datumsteil ist **immer der 1. des Monats**, unabhängig davon, wann die
Datei tatsächlich hochgeladen wurde (siehe §2.3). Der Verfügbarkeits-Check
braucht deshalb **kein** Listing-Parsing.

**Der S3-Bucket ist nicht mehr direkt nutzbar** — weder Listing noch
Einzelobjekt: **[EMPIRISCH]**

```
$ curl -s "https://discogs-data-dumps.s3.us-west-2.amazonaws.com/?delimiter=/&prefix=data/2026/"
<Error><Code>AccessDenied</Code>…                       # Listing: 403
$ curl -sI "https://discogs-data-dumps.s3.us-west-2.amazonaws.com/data/2026/discogs_20260801_CHECKSUM.txt"
HTTP/1.1 403 Forbidden                                   # Einzelobjekt: 403
```

Das ist eine bewusste Änderung Anfang 2026; im Discogs-Forum ist genau das
dokumentiert, mit `data.discogs.com` als offiziellem Weg.
**[QUELLE]** https://www.discogs.com/forum/thread/1160730
(Der Thread selbst ist hinter einer Cloudflare-JS-Challenge und war nur über
Suchmaschinen-Auszug lesbar.)

`data.discogs.com` ist **kein Redirect**, sondern ein Cloudflare-Proxy vor S3
(Antwort direkt mit `server: cloudflare` und `content-disposition:
attachment`). Daraus folgen die Einschränkungen in §2.5. **[EMPIRISCH]**

### 2.2 Maschinenlesbares Listing für den täglichen Check

Es gibt **kein** JSON/XML-Listing mehr. Verfügbare Optionen: **[EMPIRISCH]**

| Weg | Kosten | Bewertung |
|---|---|---|
| **A: HEAD auf die konstruierte `CHECKSUM.txt`-URL** | 1 Anfrage/Tag | **empfohlen** — deterministischer Name, `CHECKSUM.txt` ist der Fertig-Marker (§2.4) |
| B: HTML-Listing `?prefix=data%2FYYYY%2F` parsen | 1 Anfrage + Parser | fragil (HTML), aber liefert Größen + Zeitstempel; nur als Fallback/Diagnose |
| C: S3-`ListObjectsV2` | — | **nicht möglich** (403) |
| D: Internet-Archive-Spiegel | — | **veraltet**, siehe §2.6 |

### 2.3 Rhythmus — wann erscheint der Dump?

Zeitstempel aller acht 2026er-Dumps aus dem HTML-Listing (S3 `LastModified`,
UTC): **[EMPIRISCH]**

| Dump | labels | masters | releases | artists | CHECKSUM |
|---|---|---|---|---|---|
| 20260101 | **01-15** 16:40 | 01-15 16:54 | 01-15 16:53 | 01-15 16:48 | 01-15 16:40 |
| 20260201 | 02-01 09:15 | 02-01 11:24 | 02-01 19:19:00 | 02-01 14:11 | 02-01 **19:19:13** |
| 20260301 | 03-01 09:01 | 03-01 11:31 | 03-01 19:34:11 | 03-01 14:20 | 03-01 **19:34:24** |
| 20260401 | 04-01 08:54 | 04-01 11:25 | 04-01 19:22:07 | 04-01 14:21 | 04-01 **19:22:20** |
| 20260501 | 05-01 08:58 | 05-01 11:35 | 05-01 19:51:42 | 05-01 14:43 | 05-01 **19:51:55** |
| 20260601 | 06-01 08:54 | 06-01 11:33 | 06-01 19:48:34 | 06-01 14:36 | 06-01 **19:48:48** |
| 20260701 | 07-01 08:50 | 07-01 11:23 | 07-01 19:16:59 | 07-01 15:10 | 07-01 **19:17:09** |
| 20260801 | 08-01 09:03 | 08-01 11:20 | 08-01 19:14 | 08-**02** 16:03:41 | 08-**02** **16:03:43** |

**Ableitungen:**
- Normalfall: **1. des Monats**, komplett gegen **~19:20–19:50 UTC**.
  Reihenfolge meist labels → masters → artists → releases → CHECKSUM.
- **`CHECKSUM.txt` wird als letzte Datei geschrieben** — in 7 von 8 Monaten
  wenige Sekunden nach der letzten `.xml.gz`, im August 2 Sekunden nach
  `artists`. Das ist der belastbarste „Dump vollständig"-Marker.
- **Ausreißer sind real:** Der Januar-2026-Dump erschien erst am **15.01.**
  (14 Tage Verzug), der August-Dump wurde am **02.08.** nachgezogen
  (`artists` neu hochgeladen). → Der tägliche Check aus HANDOFF §4 ist
  nicht nur Kür, sondern nötig; die Job-Logik darf sich nicht auf „am 1. um
  20:00 ist es da" verlassen.
- Der Job muss zudem tolerieren, dass eine bereits vorhandene Datei
  **nachträglich ersetzt** wird (August: `artists` am Folgetag neu). Das
  Merkmal dafür ist der **ETag** bzw. die geänderte Prüfsumme.

### 2.4 Checksummen **[EMPIRISCH]**

`discogs_20260801_CHECKSUM.txt` (388 Bytes, 4 Zeilen):

```
f0884a52b025d3a1e001543c32097ca8c9f3021162752c36e176978ab87ae453 discogs_20260801_labels.xml.gz
11b4d472b08febf99b48709bfbafea93e8d1902efed9fad5b4ece7e819307329 discogs_20260801_masters.xml.gz
325d0ad0e3fd5be46f554a942c0314d2a34854270c08abe7579337d123f32569 discogs_20260801_releases.xml.gz
2574b62ef548c03eb83e728de1ff95a6a07b356a4b53866cb9865eedad9e6e38 discogs_20260801_artists.xml.gz
```

- Algorithmus: **SHA-256** (64 Hex-Zeichen). Verifiziert: die vollständig
  geladene `labels`-Datei hat exakt die angegebene Summe.
- **Fallstrick:** Trennzeichen ist **ein** Leerzeichen. GNU/BSD
  `sha256sum -c` bzw. `shasum -a 256 -c` erwarten **zwei**:
  ```
  $ shasum -a 256 -c c1.txt        → "no properly formatted SHA checksum lines found"
  $ sed 's/ /  /' c1.txt > c2.txt && shasum -a 256 -c c2.txt   → "…labels.xml.gz: OK"
  ```
  → Im Importer entweder selbst hashen und stringvergleichen (empfohlen)
  oder die Datei vorher normalisieren.
- Reihenfolge der Zeilen ist **nicht** alphabetisch und nicht stabil —
  nach Dateiname suchen, nicht nach Zeilennummer.
- **ETag ≠ Prüfsumme** bei den großen Dateien: `CHECKSUM.txt` hat
  `etag: "7a204ba3…"` = exakt ihr MD5 (single-part), die 86-MB-`labels`-Datei
  dagegen `etag: "1f4f187f…-11"` (Multipart-ETag über 11 Teile, **nicht** MD5
  des Inhalts). Der ETag taugt damit als **Änderungsdetektor**, nicht als
  Integritätsprüfung.

### 2.5 Transport-Eigenschaften von `data.discogs.com` **[EMPIRISCH]**

Diese vier Befunde bestimmen das Design des Downloaders:

1. **Rate-Limit mit Stundensperre.** Nach ca. 15 Anfragen in ~6 Minuten
   (davon mehrere große Downloads):
   ```
   HTTP/2 429
   content-type: application/json
   retry-after: 3574
   {"message":"You are making requests too quickly."}
   ```
   Die Sperre gilt für den gesamten Host, auch für `HEAD` auf 388-Byte-Dateien.
   → **Ein** Check-Request pro Tag; Downloads sequenziell, nie parallel;
   bei 429 den `Retry-After` respektieren und den Lauf auf den nächsten Tag
   verschieben.
2. **Kein Range/Resume.** `curl -r 0-3000000` lieferte `HTTP/2 200` und die
   **vollständige** 90.078.427-Byte-Datei; es gibt **kein**
   `accept-ranges: bytes`. → Ein Abbruch beim 10,4-GB-`releases`-Download
   bedeutet vollständigen Neustart. Der Job braucht großzügige Timeouts,
   Retry mit Backoff und muss den 429 vom Netzfehler unterscheiden.
3. **`HEAD` liefert keine `content-length`** (nur `content-disposition`,
   `etag`, Cloudflare-Header). Die Dateigröße kommt entweder aus dem
   HTML-Listing oder erst aus dem GET. → Der Plattenplatz-Guard kann sich
   nicht auf einen HEAD stützen; entweder Listing parsen (Weg B) oder
   konservativ mit den Werten aus §2.6 rechnen.
4. **Kein `content-encoding: gzip`** auf der Transportebene — der Body ist
   die `.gz`-Datei selbst. Streamende Dekompression im Importer ist also
   direkt möglich (`gzip -dc` / Python `gzip.GzipFile` über den Response-Stream).

### 2.6 Dump-Größen — Ist-Werte für §15.1 und den Plattenplatz-Guard

**Komprimiert (gz), Werte aus dem HTML-Listing, MB = MiB, G = GiB:** **[EMPIRISCH]**

| Dump | artists | labels | masters | releases | **Summe** |
|---|---|---|---|---|---|
| 20260101 | 455,8 MB | 82,2 MB | 567,2 MB | 10,30 G | **11,38 G** |
| 20260201 | 458,1 MB | 82,8 MB | 571,2 MB | 10,10 G | 11,19 G |
| 20260301 | 460,3 MB | 83,4 MB | 574,5 MB | 10,20 G | 11,29 G |
| 20260401 | 462,8 MB | 83,9 MB | 578,4 MB | 10,20 G | 11,30 G |
| 20260501 | 465,2 MB | 84,4 MB | 582,2 MB | 10,30 G | 11,41 G |
| 20260601 | 467,4 MB | 84,9 MB | 585,9 MB | 10,30 G | 11,41 G |
| 20260701 | 469,6 MB | 85,4 MB | 589,5 MB | 10,40 G | 11,52 G |
| **20260801** | **471,6 MB** | **85,9 MB** | **593,2 MB** | **10,40 G** | **11,52 G** |

Wachstum Jan→Aug 2026: artists +3,5 %, labels +4,5 %, masters +4,6 %,
releases ~+1 % (Rundung des Listings ist grob). → **ca. +6 %/Jahr**.
**[EMPIRISCH]**

**Entpackt** (Kompressionsverhältnis gemessen an den Stichproben; labels exakt,
Rest hochgerechnet): **[EMPIRISCH für labels, ANNAHME für den Rest]**

| Datei | gz | Verhältnis | entpackt | Ø Bytes/Record | Records |
|---|---|---|---|---|---|
| labels | 85,9 MB | **5,15×** (exakt) | **422 MB** (exakt: 442.488.486 B) | 184 | **2.405.196** (exakt) |
| artists | 471,6 MB | 3,45× | ≈ 1,6 GiB | 673 | ≈ 2,5 Mio. |
| masters | 593,2 MB | 5,40× | ≈ 3,1 GiB | 2.449 | ≈ 1,4 Mio. |
| releases | 10,4 GiB | 6,24× | ≈ **65 GiB** | 4.084 | ≈ 17 Mio. |
| **Summe** | **11,5 GiB** | | **≈ 70 GiB** | | |

Die Record-Zahlen für releases/masters/artists sind Hochrechnungen aus dem
**Dateianfang** (niedrige IDs = alte, sehr vollständig gepflegte Vinyl-Releases
mit vielen Credits) und deshalb eher **Unter**schätzungen. Discogs selbst nennt
**„18 Mio.+ Releases, 2,4 Mio. Artists, 2,1 Mio. Labels"**
**[QUELLE]** https://www.discogs.com/about/features/discography/ — die
Label-Zahl aus dem Dump (2,4 Mio.) liegt darüber, die Marketing-Zahlen sind
also nicht exakt. Für die Planung: **~18–19 Mio. Releases**.

**Konsequenz für den Plattenplatz-Guard (HANDOFF §15.1):**
- Download-Puffer: **≥ 12 GiB** frei (alle vier gz gleichzeitig), plus
  Reserve für einen Fehlversuch → **20 GiB** vor Start prüfen.
- Kein Bedarf für 70 GiB entpackten Zwischenspeicher, **wenn** der Importer
  streamt (`gzip`-Stream → SAX/iterparse → `COPY`), was er ohnehin muss.
- Die HANDOFF-Schätzung **„Discogs-Postgres inkl. Indizes 100–180 GB"**
  bleibt plausibel (70 GiB XML → normalisiert weniger Roh-Bytes, aber
  Indizes und Postgres-Overhead schlagen zu). Siehe §1.8 für gemessene
  Werte aus dem Referenz-Tooling.

### 2.7 Fallback-Quellen

Der Internet-Archive-Spiegel **„Dumps of DISCOGS.ORG Metadata (2008–Present)"**
existiert, ist aber **veraltet**: 146 Items, das neueste ist
`discogs-dumps-20230201` (hochgeladen 2023-07-17). **[EMPIRISCH]**
(Abfrage: `archive.org/advancedsearch.php?q=collection:discogs-dumps&sort=publicdate+desc`)
→ Nur als historische Referenz brauchbar, **nicht** als Betriebs-Fallback.
**[QUELLE]** https://archive.org/details/discogs-dumps

---

## 3. Lizenz

**Bestätigt: CC0, für alle vier Entitäten.** Der Text steht wörtlich im
Kopf von `https://data.discogs.com/` und nennt die Entitäten explizit:
**[EMPIRISCH]**

> Download Discogs Data
>
> Here you will find monthly dumps of Discogs Release, Artist, Label, and
> Master Release data. The data is in XML format and formatted according
> to the API spec: http://www.discogs.com/developers/
>
> This data is made available under the **CC0 No Rights Reserved** license:
> http://creativecommons.org/about/cc0

Damit ist die HANDOFF-§2-Aussage „Discogs-Dumps CC0" verifiziert, und zwar
**ohne** Einschränkung auf einzelne Entitäten: der Satz nennt Release, Artist,
Label und Master Release gemeinsam und bezieht „this data" darauf.

**Wichtige Abgrenzung:** Die CC0-Erklärung gilt für die **Dump-Daten**. Sie
gilt **nicht** für
- **Bilder/Artwork** (kommen nicht aus dem Dump, sondern über die API — §4),
- API-Antworten im Allgemeinen (die unterliegen den API-Nutzungsbedingungen),
- die Marketplace-Daten (nicht Teil der Dumps).

Für das Repo bedeutet das: Der lokale Spiegel der Dump-Daten ist
lizenzrechtlich unproblematisch (CC0 = keine Namensnennungspflicht, keine
Weitergabebeschränkung); die **Bilder** sind es nicht — siehe §4.4. Der
README-Hinweis aus HANDOFF §2 („keine Weiterverteilung der Bestände") bleibt
trotzdem sinnvoll, weil er die Cover/TheAudioDB-Bestände mit abdeckt.

Ergänzend hat das Internet Archive die Bulk-Freigabe durch Discogs 2020
öffentlich gewürdigt und dabei die Public-Domain-Widmung bestätigt.
**[QUELLE]** https://blog.archive.org/2020/12/06/discogs-thank-you-a-commercial-community-site-with-bulk-data-access/

---

## 4. Bilder-API

### 4.1 Rate-Limits der Discogs-API

**Dokumentierter Stand** **[QUELLE]**
https://www.discogs.com/developers/#page:home,header:home-rate-limiting
(Live-Seite für Werkzeuge per Cloudflare gesperrt; gelesen über
http://web.archive.org/web/20260106185457/https://www.discogs.com/developers):

> „Requests are throttled by the server **by source IP** to **60 per minute for
> authenticated** requests, and **25 per minute for unauthenticated** requests…
> Our rate limiting tracks your requests using a **moving average over a 60
> second window**. If no requests are made in 60 seconds, your window will reset."

**Header** (auf *jeder* Antwort, auch 404 und 429): **[EMPIRISCH]**

```
x-discogs-ratelimit: 25            # 60 mit Credentials
x-discogs-ratelimit-used: 10
x-discogs-ratelimit-remaining: 15
```

**Verhalten bei Überschreitung** **[EMPIRISCH]** (kontrollierter Burst gegen
`api.discogs.com`, 04.08.2026):

- Status **429**, **kein `Retry-After`**, kein `X-RateLimit-Reset`.
- **Keine Sperre, keine Eskalation:** der Bucket „leckt" kontinuierlich; der
  Request unmittelbar nach einem 429 lieferte wieder 200. In einem zweiten
  Lauf trat der 429 erst nach 22 statt 12 Requests auf.
- `x-discogs-ratelimit-used` springt teils um 2 pro Request (gleitender
  Durchschnitt) → **nicht als exakter Zähler behandeln**, nur
  `-remaining` als Bremssignal auswerten.
- ⚠️ **Widerspruch in den Messungen:** Eine Messreihe sah als 429-Body
  `{"message":"You are making requests too quickly."}`, eine zweite einen
  **leeren** Body (`"\n"`) bei sonst gleichen Headern. Beides ist empirisch,
  vermutlich Cloudflare-Edge vs. Origin. → Der Client darf sich **nicht** auf
  den Body verlassen, sondern muss am Status 429 hängen. (Offener Punkt Ⓞ7.)
- Das Limit ist **pro Quell-IP**, nicht pro Token → alle Prozesse des
  Containers teilen sich ein Budget. Ein zentraler Drossel-Punkt im Container
  ist Pflicht (deckt sich mit HANDOFF §11: „Queue mit Drossel").

**429 ist in der offiziellen Statuscode-Liste der Doku nicht aufgeführt**
(dort nur 200/201/204/401/403/404/405/422/500) — der Code ist real, aber
undokumentiert.

### 4.2 Auth-Formen

Offizielle Tabelle **[QUELLE]** https://www.discogs.com/developers/#page:authentication

| Credentials | Rate-Limit | Image URLs (laut Doku) | als Nutzer authentifiziert |
|---|---|---|---|
| keine | Low tier (25/min) | **nein** | nein |
| nur Consumer key/secret | High tier (60/min) | ja | nein |
| voller OAuth 1.0a | High tier | ja | ja (beliebiger Nutzer) |
| **Personal Access Token** | **High tier** | ja | ja (nur Token-Inhaber) |

→ **Für dieses Projekt reicht der Personal Access Token** (HANDOFF §2:
„Discogs-Token (für Bilder-API)"). OAuth 1.0a wird nur für Handeln im Namen
fremder Nutzer gebraucht. Übertragung (empfohlene Form, hält das Secret aus
Logs und Referrern):

```
Authorization: Discogs token=abcxyz123456
```

**User-Agent ist Pflicht.** Doku: „Your application must provide a User-Agent
string that identifies itself – preferably something that follows RFC 1945",
Format `Name/Version +URL`, z. B. `MyDiscogsClient/1.0 +http://example.org`.
Ausdrücklich als schlecht gebrandmarkt: `curl/…`, gefälschte Browser-UAs,
`my app`. Die Doku droht: „the alternative is that we just silently block it".
**[EMPIRISCH]** Ohne UA-Header antwortet heute **Cloudflare mit 403 (HTML)** —
nicht, wie die veraltete FAQ sagt, mit einer leeren Antwort.

### 4.3 Bild-URLs: woher, und braucht das Auth?

**Bestätigung der HANDOFF-Annahme — mit einer wichtigen Korrektur.**

Die Dumps enthalten **keine** Bild-URLs. Historie und aktueller Stand:

| Zeitraum | `<images>` im Dump |
|---|---|
| bis 02/2015 | vorhanden **mit** befüllten `uri`/`uri150` |
| 03/2015 | Block komplett entfernt (Nutzerprotest im Forum) |
| danach bis 2024 | Block **wieder da**, aber `uri=""` `uri150=""`; `type`/`width`/`height` befüllt |
| **ab spätestens 01/2025** | **Block ersatzlos entfernt** — auch `type`/`width`/`height` weg |

Die ersten drei Zeilen sind Forum-belegt **[QUELLE]**
https://www.discogs.com/forum/thread/411182 ·
https://www.discogs.com/forum/thread/756269 ·
https://www.discogs.com/forum/thread/852247 — die letzte Zeile ist **von uns
selbst gemessen** (§1.7): 0 Treffer für `<images` im 2025-01- und im
2026-08-Dump, gegenüber 3.248 Treffern in einer gleich großen 2020-01-Stichprobe.

> ⚠️ **Explizite Korrektur:** Verbreitete Quellen (und eine parallele
> Recherchespur dieser Untersuchung) behaupten, der `<images>`-Block sei
> „heute wieder da, nur mit leeren URIs". **Das gilt für den aktuellen Dump
> nicht mehr.** Wer darauf baut, Bildanzahl/-typ/-auflösung aus dem Dump zu
> lesen (um API-Calls zu sparen), plant an der Realität vorbei. **[EMPIRISCH]**

**Bezugsweg für Bild-URLs:** kein eigener Bild-Endpunkt, sondern das
`images[]`-Array der vier Ressourcen `/releases|masters|artists|labels/{id}`
**[QUELLE]** https://www.discogs.com/developers/#page:images:

> „Image requests require authentication… To retrieve images, authenticate via
> OAuth or Discogs Auth and fetch the object that contains the image of interest."

Felder je Eintrag: `type` (`primary`|`secondary`), `uri`, `uri150`,
`resource_url` (**identisch zu `uri`**), `width`, `height`; zusätzlich
`thumb` auf Objektebene (== `uri150`).

**[EMPIRISCH] Die Doku ist an dieser Stelle überholt:** Am 04.08.2026 lieferten
`/releases/1`, `/releases/249504`, `/artists/1`, `/masters/1000` und
`/labels/1` **ohne jedes Token** ein vollständig befülltes `images[]`.
Die Auth-Grenze greift nur noch bei `/database/search`, wo `thumb` und
`cover_image` unauthentifiziert leer bleiben.
→ **Trotzdem Token setzen.** Das Verhalten ist undokumentiert (und dessen
Nutzung laut ToS untersagt, §4.4), und der Token bringt ohnehin 60 statt
25 Requests/min.

### 4.4 Bildabruf selbst **[EMPIRISCH]**

- Domain: **`i.discogs.com`** (CNAME → Cloudflare). `img.discogs.com` zeigt auf
  dieselben IPs. **`api-img.discogs.com` ist NXDOMAIN** — alte URLs aus Doku
  und Blogposts sind tot.
- **Kein Token, kein Referer, nicht einmal ein User-Agent nötig** → 200.
- **Keine `x-discogs-ratelimit`-Header** auf Bildantworten; der Bildabruf ist
  **vom API-Limit entkoppelt** (während `api.discogs.com` 429 lieferte, gab
  `i.discogs.com` weiter 200).
- `cache-control: public, max-age=31536000` (1 Jahr), `content-type: image/jpeg`,
  `content-disposition: inline; filename="R-1-….jpg"`.
- **Signierte imgproxy-URLs:**
  `https://i.discogs.com/{SIG}/rs:fit/g:sm/q:90/h:600/w:600/{BASE64}.jpeg`,
  wobei `{BASE64}` zu `s3://discogs-database-images/R-1-….jpeg` dekodiert.
  Die Signatur deckt **den gesamten Pfad inklusive Resize-Parameter** ab:
  verfälschte Signatur → 403; unveränderte Signatur mit geändertem `h`/`w` →
  **403**. → **Eigene Größen sind nicht konstruierbar**; nur `uri`, `uri150`
  und `thumb` exakt so verwenden, wie geliefert.
- **Kein Ablaufdatum in der URL** (kein `exp=`, kein Timestamp im Payload).
  **Aber:** imgproxy-Signaturen hängen an einem serverseitigen Schlüssel —
  eine Rotation entwertet **alle** gespeicherten URLs auf einen Schlag.
  → **Bilddatei speichern, nicht die URL.** Das deckt sich mit der
  Cover-Politik aus HANDOFF §4 (ein normalisiertes JPEG pro Release).

**Drossel-Praxis für Massenabrufe:** Ein eigener Burst von 75 sequenziellen
Requests gegen 16 URLs lief **komplett ohne 429** durch — allerdings mit
`cf-cache-status: HIT`, also aus dem Edge-Cache. **[EMPIRISCH]** Für echte
Massenabrufe (viele verschiedene, ungecachte Bilder) berichten Nutzer im Forum
eine **undokumentierte Cloudflare-Drossel von ~20–30/min**, teils auch mit
Auth. **[QUELLE]** https://www.discogs.com/forum/thread/997721
Die einzige offizielle Staff-Zahl („240 per minute") stammt von 2018 und gilt
für die abgeschaltete `api-img`-Domain. **[QUELLE]**
https://www.discogs.com/forum/thread/709515
→ **Empfehlung: konservativ 1 Bild/Sekunde**, exponentielles Backoff mit
Jitter bei 429 (es gibt kein `Retry-After`). Das entspricht dem, was
`python3-discogs-client` seit 2.3.10 standardmäßig tut. **[QUELLE]**
https://github.com/joalla/discogs_client/pull/34

### 4.5 ToS-Grenzen fürs Cachen — **das ist der kritische Befund**

Maßgeblich: **Discogs API Terms of Use**, „Last Updated: May 27th, 2025".
**[QUELLE]** https://support.discogs.com/hc/en-us/articles/360009334593-API-Terms-of-Use
(Cloudflare-gesperrt; gelesen über
http://web.archive.org/web/20251118015920/https://support.discogs.com/hc/en-us/articles/360009334593-API-Terms-of-Use)

**1. Es gibt keine Caching-Erlaubnis, sondern zwei Grenzen** (wörtlich):

> „The Content within Our API is dynamic and is quickly outdated. You may not
> display in any format or to any audience the Content if it is **more than six
> (6) hours older** than the information on Our online properties or
> applications. **You may not cache or store the Content longer than is
> necessary to provide a service to Your application's users.**"

**2. Bilder sind ausdrücklich NICHT CC0.** Die ToS teilen zwei Klassen:
- **CC0 Data:** „Release titles, notes, dates, format, track listings, barcodes
  and other identifiers, credits, versions, URL links… / Artist names, notes,
  associated releases / Label, producer, manufacturer, distributor… names and
  contact information, notes, and associated releases".
- **Restricted Data:** ausdrücklich **„Images"** (Release Images, Artist Images,
  Label Images, User Images), außerdem Discogs User Data und Marketplace Data.

Für Restricted Data gilt (wörtlich):
> „— **Transfer Restricted Data to any third party.**
> — **Use Restricted Data for any commercial purposes.**"

**3. Zwei Pflicht-Attributionen** (wörtlich):
> „'This application uses Discogs' API but is not affiliated with, sponsored or
> endorsed by Discogs. "Discogs" is a trademark of Zink Media, LLC.'"

> „In addition, You must display the following notice **directly next to any
> data** You use from the Discogs API: **'Data provided by Discogs.'** The
> notice must include a **hyperlink to the discogs.com page** that includes the
> data. The link back must **not** use any mechanism that prevents passing along
> search engine ranking credit to that page, such as **'nofollow'**."

**4. Weitere einschlägige Verbote:**
> „Attempt to or actually replicate… or access any part of Our API, **including
> undocumented functionality**…"
> „**Attempt to or actually circumvent Our rate limits, such as by creating
> additional API keys** to overcome these limits."

**5. Sanktion:** „we may revoke Your API access or Your account privileges
(**including Your related accounts**)" — ohne Vorwarnung, nach Ermessen.

#### Bewertung für musicmeta-offline

| Vorhaben | Bewertung |
|---|---|
| **Dump-Spiegel** (Releases/Masters/Artists/Labels, dauerhaft, offline) | **unproblematisch** — CC0, ToS der *API* greifen nicht auf die Dumps |
| **GET-Subset aus dem Dump-Spiegel** ausliefern | **unproblematisch**, solange die Daten aus dem Dump stammen und nicht aus der API |
| **Lazy-Bilder-Cache** (Bild einmal holen, dauerhaft lokal halten) | **kollidiert** mit „may not cache or store the Content longer than is necessary" und mit „Restricted Data" |
| Bilder an Dritte weitergeben (öffentliches Deployment, Repo-Bundle) | **verboten** („Transfer Restricted Data to any third party") |
| Kommerzielle Nutzung der Bilder | **verboten** (allgemeine kommerzielle API-Nutzung ist sonst erlaubt) |

Die 6-Stunden-Frischeregel ist so weit gefasst, dass praktisch **jeder**
persistente API-Cache angreifbar ist. Für ein rein privates, im LAN
betriebenes Werkzeug (der Betriebsfall laut HANDOFF §2) ist das Risiko
gering; sobald der Container öffentlich exponiert oder das Ergebnis geteilt
wird, kollidiert insbesondere die **Bildspeicherung** mit den
Restricted-Data-Regeln. → Entscheidungsbedarf, siehe Ⓞ8.

**Wichtige Entlastung:** Da Discogs in der Cover-Kette nur die **dritte**
Stufe ist (CAA → TheAudioDB → Discogs, HANDOFF §4/§7) und der CAA-Voll-Spiegel
den Löwenanteil abdeckt, ist das Discogs-Bildvolumen von vornherein klein.
Die Kollision betrifft also einen Randfall, nicht den Kernbestand.

---

## 5. Antwortformat des GET-Subsets — Nachbau-Spezifikation

Alle Feldangaben in diesem Abschnitt stammen aus **eigenen, unauthentifizierten
`curl`-Abrufen gegen `api.discogs.com` am 04.08.2026** (Stichprobe: 16 Releases,
3 Masters, 6 Artists, 4 Labels; darunter gezielt Releases mit
`index`/`heading`-Tracks und `sub_tracks`: 2674825, 3664623, 1027881, 7355025).
**[EMPIRISCH]** Die offizielle Doku unter https://www.discogs.com/developers/
ist für Werkzeuge per Cloudflare gesperrt (403) und war nur über einen
Wayback-Snapshot lesbar; ihre Beispiel-Bodies stammen aus **2014** und sind an
mehreren Stellen überholt. **[QUELLE]**
https://web.archive.org/web/20210601000000/https://www.discogs.com/developers/resources/database/release.html

### 5.1 HTTP-Rahmen, den der Spiegel nachbilden muss **[EMPIRISCH]**

- `content-type: application/json` (ohne charset), UTF-8.
- **Kein `ETag`, kein `Last-Modified`** bei allen vier Endpunkten — bedingte
  GETs sind im Original nicht möglich. Der Spiegel *darf* besser sein.
- `gzip` nur auf Anforderung (`accept-encoding`); `vary: Accept-Encoding` ist
  immer gesetzt.
- **Header-Asymmetrie:** `/releases` und `/masters` liefern einen minimalen
  Header-Satz; `/artists` und `/labels` zusätzlich CORS-Header,
  `cache-control: public, must-revalidate`, `content-language: en`,
  `x-content-type-options: nosniff` und `x-discogs-media-type: discogs.v2`.
- **Rate-Limit-Header liegen auf jeder Antwort an**, auch auf 404 und 429:
  `x-discogs-ratelimit: 25` (unauthentifiziert), `-used`, `-remaining`.
- **Fehler-Bodies sind je Entität unterschiedlich formuliert** und müssen
  wörtlich nachgebildet werden:
  | Fall | Status | Body |
  |---|---|---|
  | Release unbekannt/gelöscht | 404 | `{"message":"That release does not exist or may have been deleted."}` |
  | Master unbekannt | 404 | `{"message":"That master release does not exist or may have been deleted."}` |
  | Artist unbekannt | 404 | `{"message":"Artist not found."}` |
  | Label unbekannt | 404 | `{"message":"Label not found"}` (**ohne** Schlusspunkt) |
  | falsche Methode | 405 | `{"message":"Method PUT is not allowed for this resource."}` |
  | Limit überschritten | 429 | **leerer Body** (`"\n"`), kein `Retry-After` |
- **Keine Redirects** bei zusammengeführten IDs beobachtet (getestet mit
  `--max-redirs 0` auf 12 IDs): immer 200 oder 404, nie 3xx.
- **JSONP** über `?callback=cb` ist aktiv:
  `cb({"meta":{"status":200},"data":{…}})`.
- **`?curr_abbr=`** ist unauthentifiziert **wirkungslos** (Preis bleibt in USD),
  ungültige Werte werden ignoriert (200 statt Fehler).
- **Ohne `User-Agent`-Header: 403 mit Cloudflare-HTML** — nicht die in der
  Doku beschriebene leere Antwort.

### 5.2 Der zentrale Befund: Feld-**Weglassung** ist bedeutungstragend

Discogs sendet bei „nichts vorhanden" **keinen `null`-Wert, sondern lässt den
Schlüssel weg**. Betroffen (empirisch belegt): `master_id`/`master_url`,
`extraartists`, `companies`, `parent_label`, `sublabels`, `members`, `groups`,
`aliases`, `realname`, `urls`, `images`, `notes` (Master), `videos` (**nur beim
Master**; beim Release steht dann `[]`). Einzige beobachtete echte
`null`-Ausnahme: `lowest_price`.

→ Ein Spiegel, der überall `null` schreibt, bricht Clients, die
`"key" in obj` prüfen. Das ist die wichtigste einzelne Nachbau-Regel.

### 5.3 Feldlisten der vier Endpunkte

**`GET /releases/{id}`** — Skalare: `id`(int), `status`(str), `title`,
`artists_sort`, `year`(int, 0=unbekannt), `released`(Teildatum-String mit
`-00`), `released_formatted`, `country`, `notes`, `uri`, `resource_url`,
`thumb`, `data_quality`, `date_added`/`date_changed` (ISO 8601 mit Offset
`-07:00`), `estimated_weight`(int, Gramm), `format_quantity`(int),
`blocked_from_sale`(bool), `is_offensive`(bool), `num_for_sale`(int),
`lowest_price`(float|null), `master_id`(int), `master_url`(str).
Arrays: `genres[]`, `styles[]` (Strings); `artists[]`, `extraartists[]`
(`id,name,anv,join,role,tracks,resource_url` + bei `artists[]` optional
`thumbnail_url`); `labels[]`, `companies[]`, `series[]`
(`id,name,catno,entity_type,entity_type_name,resource_url,thumbnail_url` —
`resource_url` zeigt bei **allen dreien** auf `/labels/{id}`);
`formats[]` (`name`, `qty` als **String**, `descriptions[]`, `text`);
`identifiers[]` (`type,value,description`); `images[]`
(`type,uri,uri150,resource_url(==uri),width,height`); `videos[]`
(`uri,title,description,duration(int),embed(bool)`);
`community` (`have,want,rating{count,average},status,data_quality,
submitter{username,resource_url},contributors[]`); `tracklist[]` (§5.4).

**`GET /masters/{id}`** — `id`, `title`, `year`, `main_release`,
`main_release_url`, `most_recent_release`, `most_recent_release_url`,
`versions_url`, `resource_url`, `uri`, `data_quality`, `num_for_sale`,
`lowest_price`, `genres[]`, `styles[]`, `notes`(optional), `artists[]`
(inkl. `thumbnail_url`), `tracklist[]`, `images[]`, `videos[]` (Schlüssel
fehlt bei Leere). **Nicht** vorhanden: `country`, `released`, `labels`,
`formats`, `identifiers`, `companies`, `community`, `status`, `date_*`,
`thumb`, `series`, `extraartists`, `artists_sort`.

**`GET /artists/{id}`** — `id`, `name`, `resource_url`, `uri`,
`releases_url`, `profile`, `data_quality`, `namevariations[]` (Strings),
optional: `realname`, `urls[]`, `images[]`, `aliases[]`
(`id,name,resource_url` — **ohne** `active`), `members[]`/`groups[]`
(`id,name,resource_url,active(bool)`).

**`GET /labels/{id}`** — `id`, `name`, `resource_url`, `uri`,
`releases_url`, `data_quality`, optional: `profile`, `contact_info`
(mit `\r\n`), `urls[]`, `images[]`, `sublabels[]` (`id,name,resource_url`),
`parent_label` (`id,name,resource_url` — **Objekt**, nicht Array).

### 5.4 Tracklist — API vs. Dump

Die API liefert pro Eintrag `type_` ∈ {`track`,`index`,`heading`},
`position`, `title`, `duration` (alle `""`-fähig) und optional `artists[]`,
`extraartists[]`, `sub_tracks[]`. `heading` ist ein reiner Abschnittstitel
ohne Container-Funktion; `index` ist ein Container, dessen Kinder in
`sub_tracks` stehen. **[EMPIRISCH]**

Der Dump kennt **kein `type_`** (§1.6). Ableitungsregel für den Spiegel:

```
sub_tracks vorhanden                          -> "index"
kein <position> UND kein <duration>           -> "heading"
sonst                                         -> "track"
```

Verifiziert an Release 3664623 (alle drei Typen gemischt) und 2674825
(`index` mit aggregierter Dauer `23:01` und 9 `sub_tracks`-Einträgen).

### 5.5 **Delta-Tabelle Dump → API** (die eigentliche Nachbau-Spezifikation)

Legende: **1:1** = direkt aus dem Dump; **ABL** = aus Dump-Daten ableitbar;
**KONST** = konstant zu setzen; **EXT** = braucht eine externe Quelle
(Bilder-API bzw. Marketplace); **WEG** = im Spiegel weglassen (Schlüssel
nicht senden).

#### Release

| API-Feld | Dump-Quelle | Bewertung |
|---|---|---|
| `id`, `title`, `country`, `notes`, `data_quality` | `@id`, `<title>`, `<country>`, `<notes>`, `<data_quality>` | **1:1** |
| `released` | `<released>` | **1:1** (Teildatum-String, deckt sich exakt) |
| `year` | erste 4 Zeichen von `<released>`, sonst `0` | **ABL** |
| `released_formatted` | aus `<released>` (`"Jul 1987"`, `"1987"`) | **ABL**, Formatierung nachbauen |
| `status` | **existiert im Dump nicht mehr** (§1.7) | **KONST** `"Accepted"` |
| `artists[]`, `extraartists[]` | `<artists>/<artist>`, `<extraartists>/<artist>` | **1:1**; fehlende `<id>` → `id: 0`; `anv/join/role/tracks` als `""` auffüllen, wenn nicht im Dump |
| `artists_sort` | Konkatenation von `artists[].name`+`join` | **ABL** (Original bildet exakt die Anzeigezeile) |
| `labels[]` | `<labels>/<label @id @name @catno>` | **1:1**; `entity_type`/`entity_type_name` **KONST** `"1"`/`"Label"` (Dump liefert sie nicht) |
| `series[]` | `<series>/<series @id @name @catno>` | **1:1**; `entity_type`/`entity_type_name` **KONST** `"2"`/`"Series"` |
| `companies[]` | `<companies>/<company>` | **1:1** inkl. `entity_type`+`entity_type_name` |
| `formats[]` | `<formats>/<format @name @qty @text>` + `<descriptions>` | **1:1**; `qty` als **String** ausgeben |
| `format_quantity` | Summe der `@qty` | **ABL** |
| `identifiers[]` | `<identifiers>/<identifier @type @value @description>` | **1:1** |
| `genres[]`, `styles[]`, `videos[]` | 1:1 | **1:1**; `videos` immer als Array senden (ggf. `[]`) |
| `tracklist[]` | `<tracklist>` + Ableitung `type_` (§5.4) | **ABL** |
| `master_id`, `master_url` | `<master_id>`; Wert `0` = kein Master | **ABL**: bei `0` **beide Schlüssel weglassen** |
| `estimated_weight` | — | **WEG** (nicht im Dump) |
| `date_added`, `date_changed` | — | **WEG** |
| `num_for_sale`, `lowest_price`, `blocked_from_sale` | — (Marketplace) | **WEG** — siehe Ⓞ3 |
| `is_offensive` | — | **WEG** |
| `community.*` (have/want/rating/status/submitter/contributors) | — | **WEG** — siehe Ⓞ3 |
| `images[]`, `thumb`, `*.thumbnail_url` | — (Dump hat kein `<images>` mehr) | **EXT** über den Bilder-Cache (§4) — siehe Ⓞ4 |
| `resource_url`, `uri`, `versions_url`, `releases_url` | konstruiert | **ABL** — siehe Ⓞ2 (Basis-URL) |

#### Master

| API-Feld | Dump-Quelle | Bewertung |
|---|---|---|
| `id`, `title`, `notes`, `data_quality`, `genres[]`, `styles[]`, `videos[]` | 1:1 | **1:1** (`videos`-Schlüssel bei Leere **weglassen**!) |
| `year` | `<year>` | **1:1**, Wert `0` beibehalten |
| `main_release`, `main_release_url` | `<main_release>` | **1:1**/**ABL** |
| `artists[]` | `<artists>` | **1:1** |
| `tracklist[]` | **existiert beim Master im Dump NICHT** | **ABL** aus dem `main_release` — siehe Ⓞ5 |
| `most_recent_release(_url)` | — | **WEG** (nicht zuverlässig ableitbar) |
| `num_for_sale`, `lowest_price` | — | **WEG** |
| `images[]` | — | **EXT** |
| `versions_url`, `resource_url`, `uri` | konstruiert | **ABL** |

#### Artist

| API-Feld | Dump-Quelle | Bewertung |
|---|---|---|
| `id`, `name`, `profile`, `data_quality` | `<id>`, `<name>`, `<profile>`, `<data_quality>` | **1:1** |
| `realname` | `<realname>` | **1:1**, bei Fehlen Schlüssel weglassen |
| `namevariations[]` | `<namevariations>/<name>` (Strings, ohne id) | **1:1** |
| `urls[]` | `<urls>/<url>` | **1:1** |
| `aliases[]` | `<aliases>/<name @id>` | **1:1** → `{id,name,resource_url}` |
| `members[]`, `groups[]` | `<members>/<name @id>`, `<groups>/<name @id>` | **1:1**, aber **`active` fehlt im Dump** → **KONST** `true` (siehe Ⓞ6) |
| `images[]` | — | **EXT** |
| `resource_url`, `uri`, `releases_url` | konstruiert | **ABL** |

#### Label

| API-Feld | Dump-Quelle | Bewertung |
|---|---|---|
| `id`, `name`, `profile`, `data_quality` | 1:1 | **1:1** (`name` kann fehlen, s. Fallstrick 10) |
| `contact_info` | `<contactinfo>` | **1:1** (Feldname ändert sich: `contactinfo` → `contact_info`) |
| `urls[]` | `<urls>/<url>` | **1:1** |
| `sublabels[]` | `<sublabels>/<label @id>` | **1:1** |
| `parent_label` | `<parentLabel @id>` | **1:1** (Element → **Objekt**, nicht Array) |
| `images[]` | — | **EXT** |
| `resource_url`, `uri`, `releases_url` | konstruiert | **ABL** |

### 5.6 Zusammenfassung der Lücken

**Aus dem Dump grundsätzlich nicht rekonstruierbar** (7 Gruppen):
1. **Marktdaten** — `num_for_sale`, `lowest_price`, `blocked_from_sale`.
2. **Community-Daten** — `have`, `want`, `rating`, `submitter`, `contributors`.
3. **Bilder** — `images[]`, `thumb`, `thumbnail_url` (nur über die API, §4).
4. **Zeitstempel** — `date_added`, `date_changed`.
5. **Redaktionsstatus** — `status`, `community.status`, `is_offensive`
   (seit 2026 auch nicht mehr als `status`-Attribut im Dump).
6. **Physikalisches** — `estimated_weight`.
7. **Master-Sekundärdaten** — `most_recent_release(_url)`, Master-`tracklist`.

**Feldnamens-Abweichungen Dump ↔ API** (vollständig):
`contactinfo`→`contact_info`, `parentLabel`→`parent_label`,
`master_id@is_main_release`→(entfällt, API kennt es nicht am Release),
`video@src`→`videos[].uri`, `format@text`→`formats[].text`,
`<track>` ohne `type_`→`tracklist[].type_`,
`<artist><join>`→`artists[].join` (Dump lässt es weg, API sendet `""`).

**Typ-Fallen beim Nachbau:** `formats[].qty` und `entity_type` sind
**Strings**; `videos[].duration` ist **int** (Dump: Attribut-String);
`videos[].embed` ist **bool** (Dump: `"true"`); `community.rating.average`
ist Float; `year` ist int.

---

## 6. Offene Punkte (mit Optionen + Empfehlung)

Jeder Punkt ist eine **Entscheidung, die vor bzw. während M3 fallen muss**.
Format: Optionen mit Konsequenz, danach die markierte Empfehlung.

---

**Ⓞ1 — Übergangsmonat des `status`-Attributs** *(teilweise gelöst)*

Für `<images>` ist der Umschaltpunkt **geklärt: Dump 20241201**
(discogs-xml2db Issue #155, §1.8) — deckt sich exakt mit unserer Messung
(2024-01 vorhanden, 2025-01 weg). Für `status` bleibt nur „zwischen 2025-01
und 2026-08"; die Eingrenzung scheiterte an der Stundensperre von
`data.discogs.com`.

- **A:** Nicht weiter eingrenzen — der Parser behandelt beide Felder ohnehin
  als optional.
- **B:** Später nachmessen (~4 Anfragen, über mehrere Tage verteilt).
- **C:** Importer strikt gegen den 2026er-Dump bauen (Feld gar nicht kennen).

→ **✅ Empfehlung: A.** Der Umschaltpunkt hat für den Betrieb keinen Wert;
der Parser muss `status` ohnehin als optional lesen (billig) — und **die
API liefert `status` weiterhin**, der Spiegel muss es also synthetisieren
(§5.5, Ⓞ3). C wäre unnötig fragil, falls Discogs das Feld zurückbringt.

---

**Ⓞ2 — `resource_url`/`uri`/`releases_url` im Spiegel: lokal oder Original?**

Die API liefert absolute URLs auf `api.discogs.com` bzw. `www.discogs.com`.

- **A: auf den lokalen Spiegel umschreiben** (`http://host:port/discogs/…`).
  Clients, die Links folgen, bleiben offline — das ist der Projektzweck
  (HANDOFF §1: „ohne laufende Abhängigkeit von den öffentlichen Diensten").
  Nachteil: `uri` (die Web-Seite) hat lokal keine Entsprechung.
- **B: 1:1 Original-URLs ausliefern.** Byte-genau kompatibel, aber jeder
  Client, der `resource_url` folgt, verlässt den Offline-Betrieb.
- **C: gemischt** — `resource_url`/`releases_url`/`versions_url`/`master_url`
  lokal umschreiben, `uri` (Web-Seite) im Original lassen.

→ **✅ Empfehlung: C.** `resource_url` & Co. sind API-Referenzen und gehören
in den Spiegel; `uri` ist eine reine Menschen-Verlinkung auf discogs.com und
erfüllt sogar die ToS-Attributionspflicht („hyperlink to the discogs.com
page", §4.5). Die Basis-URL muss konfigurierbar sein (analog zum bestehenden
`base_url`-Muster der anderen Endpunkte).

---

**Ⓞ3 — Nicht rekonstruierbare Felder (Marktdaten, Community, Zeitstempel)**

Betrifft `num_for_sale`, `lowest_price`, `blocked_from_sale`, `is_offensive`,
`community.*`, `date_added`, `date_changed`, `estimated_weight`,
`most_recent_release(_url)`.

- **A: Schlüssel weglassen.** Entspricht dem Discogs-Muster (§5.2) und ist
  ehrlich: „diese Information hat der Spiegel nicht".
- **B: mit Neutralwerten füllen** (`num_for_sale: 0`, `lowest_price: null`,
  `community: {have:0, want:0, …}`). Strukturell vollständiger, aber der
  Client kann echte „0 Angebote" nicht von „unbekannt" unterscheiden.
- **C: Marktdaten live von Discogs nachladen.** Widerspricht dem
  Offline-Ziel und der 6-Stunden-ToS-Regel.

→ **✅ Empfehlung: A für alles, B als Ausnahme für `community.status`
(`"Accepted"`) und `status` (`"Accepted"`).** Diese beiden sind für Clients
oft Pflichtfelder und haben im Dump-Kontext eine eindeutige Bedeutung:
Was im Dump steht, ist akzeptiert. C ist ausgeschlossen.

---

**Ⓞ4 — `images[]` / `thumb` / `thumbnail_url` im GET-Subset**

Der Dump liefert nichts; die Bilder liegen im Lazy-Cache (§4).

- **A: Schlüssel weglassen.** Kein Risiko, kein Aufwand.
- **B: aus dem lokalen Cover-Cache befüllen** — `uri`/`uri150` zeigen auf
  `/v1/cover/{mbid}` bzw. den lokalen Cover-Endpunkt. Aber: der Cover-Cache
  ist MBID-indiziert (HANDOFF §8, `covers.artwork` PK `release_mbid`), nicht
  Discogs-ID-indiziert — es bräuchte den Rückweg über `mbref`.
- **C: Original-`i.discogs.com`-URLs durchreichen**, aus einem eigenen
  URL-Cache. Bricht den Offline-Betrieb und speichert Restricted Data.

→ **✅ Empfehlung: A für M3.** B kann in **M4/M7** nachgezogen werden, wenn
das Cover-Subsystem und die MBID↔Discogs-Auflösung stehen — dann als
zusätzlicher, klar dokumentierter Nicht-Standard. C ist auszuschließen.

---

**Ⓞ5 — `masters/{id}.tracklist`: der Dump hat beim Master keine Tracklist**

Die API liefert für Master eine Tracklist; der Dump nicht (§1.5).

- **A: Tracklist des `main_release` übernehmen.** Genau das tut Discogs
  faktisch auch (der Master erbt die Anzeige vom Hauptrelease). Ein Join,
  keine zusätzlichen Daten.
- **B: leeres Array `[]`.** Ehrlich, aber Clients, die Master-Tracklisten
  erwarten, laufen leer.
- **C: Schlüssel weglassen.** Im Original ist er immer da (3/3 der Stichprobe)
  — Abweichung vom Original.

→ **✅ Empfehlung: A.** Der Join ist billig, das Ergebnis stimmt in der
Praxis mit dem Original überein, und `main_release` ist im Dump lückenlos
vorhanden (13.214/13.214 in der Stichprobe). Abweichung dokumentieren:
`position`/`duration` können vom Original abweichen, wenn Discogs den
Master abweichend pflegt.

---

**Ⓞ6 — `members[].active` / `groups[].active` fehlt im Dump**

Die API liefert ein `active`-Flag (belegt: John Lennon → The Beatles
`active:true`, Plastic Ono Band `active:false`); der Dump liefert nur
`<name id=…>`.

- **A: konstant `true`.** Häufigster Fall, ein Feld weniger im Schema.
- **B: Schlüssel weglassen.** Im Original ist er bei `members`/`groups`
  immer da.
- **C: über die API nachladen.** Widerspricht dem Offline-Ziel.

→ **✅ Empfehlung: A** mit Doku-Hinweis in der README/API-Doku, dass
`active` im Spiegel nicht aussagekräftig ist. B würde Clients brechen, die
das Feld voraussetzen.

---

**Ⓞ7 — Widersprüchliche 429-Bodies bei `api.discogs.com`**

Zwei unabhängige Messreihen am selben Tag sahen einmal
`{"message":"You are making requests too quickly."}`, einmal einen leeren
Body — bei sonst gleichen Headern.

- **A: Client hängt ausschließlich am Status 429**, Body wird ignoriert.
- **B: Beide Formen parsen.**
- **C: nachmessen.**

→ **✅ Empfehlung: A.** Der Body trägt keine verwertbare Information (es gibt
kein `Retry-After`); die Backoff-Logik braucht nur den Status. Kein
Klärungsbedarf.

---

**Ⓞ8 — ToS-Konflikt: dauerhafter Bilder-Cache vs. „6-Stunden-Regel" und
„Restricted Data"** ⚠️ *Betreiber-Entscheidung nötig*

Die API-ToS (§4.5) verbieten, API-Content länger zu speichern „than is
necessary", verlangen ≤ 6 h Frische für Angezeigtes, stufen **Bilder als
Restricted Data** ein (keine Weitergabe an Dritte, keine kommerzielle
Nutzung). Der geplante Lazy-Cache (HANDOFF §4: „offline nach Erstabruf")
kollidiert damit.

- **A: Discogs-Bildquelle bleibt wie geplant, aber standardmäßig AUS.**
  `discogs.token` ist bereits leer voreingestellt („leer = Discogs-Bildquelle
  aus", HANDOFF §10) — der Betreiber schaltet sie bewusst frei. README-Hinweis
  auf die ToS-Lage; Bilder werden nie mit ausgeliefert/mitverteilt.
- **B: Ablaufdatum auf Discogs-Bilder im Cache** (z. B. 30 Tage, danach
  Neuabruf). Nähert sich dem ToS-Geist an, kostet aber wiederkehrende
  API-Calls und untergräbt den Offline-Anspruch.
- **C: Discogs ganz aus der Cover-Kette nehmen** (nur CAA → TheAudioDB).
  Rechtlich sauber, kostet Abdeckung im Randbereich.
- **D: Nur die Bild-URL cachen, nicht das Bild.** Nutzlos: Die
  imgproxy-Signaturen können bei Schlüsselrotation kollektiv ungültig werden
  (§4.4), und offline hilft eine URL nicht.

→ **✅ Empfehlung: A.** Der Default-Aus-Zustand ist bereits im HANDOFF
verankert, das Volumen ist klein (dritte Stufe der Kette hinter dem
CAA-Voll-Spiegel), und der Betrieb ist privat/LAN. **Wichtig:** Die
Repo-Doku muss explizit sagen, dass Discogs-Bilder Restricted Data sind und
weder weiterverteilt noch kommerziell genutzt werden dürfen — das ergänzt
den bestehenden README-Hinweis aus HANDOFF §2. Zusätzlich sind die **zwei
Attributionstexte** aus §4.5 in die Admin-UI/README aufzunehmen, sobald die
Discogs-Quelle aktiv ist.

---

**Ⓞ9 — Download ohne Resume: 10,4 GB am Stück**

`data.discogs.com` unterstützt kein `Range` (§2.5); ein Abbruch bedeutet
Neustart, und ein 429 sperrt eine Stunde.

- **A: Sequenziell mit großzügigem Timeout + Retry (3 Versuche, exponentiell,
  bei 429 Abbruch bis zum nächsten Tag).** Einfach; im schlechtesten Fall
  verzögert sich der Monatsimport um Tage — bei monatlichem Rhythmus egal.
- **B: Zusätzlich über eine zweite Quelle absichern.** Es gibt keine
  (IA-Spiegel endet 2023, §2.7).
- **C: Download in einen persistenten Staging-Ordner**, damit ein
  Container-Neustart nicht die bereits geladenen drei kleinen Dateien
  verwirft.

→ **✅ Empfehlung: A + C.** C ist billig (Staging-Ordner auf dem Array, in
`dump_state` protokolliert) und rettet bei einem Neustart ~1,1 GB und drei
Downloads. Der Importer sollte pro Entität einzeln „geladen + Prüfsumme ok"
vermerken, damit nach einem Abbruch nur die fehlende Datei erneut gezogen wird.

---

**Ⓞ10 — Verfügbarkeits-Check: HEAD-Semantik auf noch nicht existierende
Dumps nicht verifiziert**

Weg A aus §2.2 (1 HEAD/Tag auf die konstruierte `CHECKSUM.txt`-URL) setzt
voraus, dass eine noch nicht existierende Datei einen unterscheidbaren Status
liefert (404 erwartet). Das konnte wegen der Stundensperre nicht geprüft
werden — beide Testabrufe endeten in 429.

- **A: Weg A implementieren und den Status defensiv behandeln**
  (200 = da, 404 = noch nicht da, 429 = heute nicht mehr fragen,
  alles andere = Fehler + Log).
- **B: HTML-Listing parsen (Weg B)** — liefert nebenbei Größe und Zeitstempel,
  ist aber gegen HTML-Änderungen anfällig.
- **C: beides** — HEAD als Tages-Check, Listing nur einmal, wenn der HEAD
  „da" meldet (für Größen zur Plattenplatz-Prüfung).

→ **✅ Empfehlung: C.** Der HEAD kostet eine Anfrage/Tag und hält den
Rate-Limit-Verbrauch minimal; das Listing wird nur einmal pro Monat geparst,
genau dann, wenn ohnehin ein Import ansteht — und liefert dabei die
`content-length`, die der HEAD nicht hergibt (§2.5). Der 404-Fall ist beim
ersten Monatswechsel praktisch zu verifizieren.

---

**Ⓞ11 — Import-Strategie: Voll-Ersetzung oder Upsert?**

Der Dump ist ein Vollstand, kein Delta.

- **A: Voll-Ersetzung in Schattentabellen**, am Ende atomarer Schema-/
  Tabellen-Switch. Sauber, resumierbar, keine Halbzustände — braucht aber
  **doppelten Plattenplatz** (~200–360 GB statt 100–180 GB laut §15.1).
- **B: `TRUNCATE` + `COPY` in dieselben Tabellen.** Kein doppelter Platz,
  aber der Spiegel ist während des Imports **leer** — verstößt gegen das
  Prinzip „Stale statt Sperre" (HANDOFF §11.3).
- **C: Upsert je Entität** (`INSERT … ON CONFLICT`) + Löschen verwaister IDs.
  Kein doppelter Platz, Spiegel bleibt durchgehend bedienbar — aber
  deutlich langsamer (kein reines `COPY`) und komplexer.

→ **✅ Empfehlung: A, mit Etappen-Switch pro Entität.** „Stale statt Sperre"
ist eine ausdrückliche Architekturregel (HANDOFF §11.3), und A ist die einzige
Option, die sie ohne Performanceverlust erfüllt. Der Platzbedarf lässt sich
entschärfen, indem **pro Entität** geladen und umgeschaltet wird
(`release_new` → Switch → `release_old` droppen): dann wird nur der größte
Einzelbestand doppelt gehalten, nicht das ganze Schema. **Das ist eine
Korrektur an der §15.1-Schätzung** — siehe §2.6.

---

**Ⓞ12 — `data_quality` und `notes` sind Freitext, kein Enum**

Beobachtet: `Correct`, `Needs Vote`, `Needs Major Changes`, `Complete and
Correct`. Kein dokumentiertes Vokabular.

→ **✅ Empfehlung:** als `text` speichern und 1:1 durchreichen; **kein**
Postgres-Enum, sonst bricht ein neuer Wert den Monatsimport. (Keine
Alternative sinnvoll — hier ist nichts zu entscheiden, nur zu beachten.)

---

**Ⓞ13 — Verhältnis zum Referenz-Tooling: Vorlage, Abhängigkeit oder Fork?**

- **A: Eigener Importer, `discogs-xml2db/develop` nur als Schema- und
  Fallstrick-Vorlage.** Passt zum Ein-Container-Modell (HANDOFF §3: „kein
  Compose-Stack"), zur Etappen-/Resume-Anforderung (§11.3) und zu
  `dump_state` (§8). Kein fremdes CSV-Zwischenformat, kein zweiter
  Sprach-Stack.
- **B: `discogs-xml2db` als Abhängigkeit einbinden.** Spart Parser-Arbeit,
  bringt aber CSV-Staging (zusätzlich ~50–100 GB, HANDOFF §15.1),
  Python-Toolchain im Image und ein Schema, das für uns 4 tote
  `*_image`-Tabellen und eine gemischte `release_artist` mitbringt.
- **C: `discogskit` einbinden** (3–9× schneller, direkter COPY-Pfad).
  Verlockend, aber v0.1.0, ein Maintainer, seit Ende 2025 — für einen
  monatlichen Kernprozess zu jung.
- **D: `discogs-load` verwenden.** Ausgeschlossen — Schema deckt Tracklist,
  Formats, Identifiers und Release-Artists nicht ab (§1.8).

→ **✅ Empfehlung: A.** Das Schema aus §1.9 folgt `discogs-xml2db/develop`
in der Struktur, korrigiert aber dessen bekannte Schwächen (keine
`*_image`-Tabellen, `artists`/`extraartists` getrennt, `sub_tracks` über
`parent_id` statt Textspalte, konsequent nullable Credit-IDs). Von
`discogskit` sollte man die **Beschleunigungstechniken** übernehmen
(`UNLOGGED` während des Ladens, Indizes parallel danach, `max_wal_size`
temporär hochsetzen) — das ist auf Array-Spindeln der Hebel, nicht die
Parser-Sprache. C bleibt als späterer Benchmark-Vergleich interessant.

**Zusatz-Entscheidung (aus #125):** FK-Constraints laufen auf echten Dumps
**nicht** sauber durch (verwaiste `artist_id`, `company_id`). → FKs nur auf
den harten Beziehungen setzen (`release_track.release_id`,
`*_url.*_id` …), **nicht** auf Credit-/Company-Verweise; dort nur Index.

---

## 7. Quellenverzeichnis

**Empirisch abgefragte Endpunkte (04.08.2026)**
- `https://data.discogs.com/` (Startseite mit Lizenztext)
- `https://data.discogs.com/?prefix=data%2F2026%2F` (Jahreslisting)
- `https://data.discogs.com/?download=data%2F2026%2Fdiscogs_20260801_{CHECKSUM.txt,labels.xml.gz,masters.xml.gz,artists.xml.gz,releases.xml.gz}`
- historische Vergleichs-Stichproben: `…/data/2020/discogs_20200101_releases.xml.gz`,
  `…/data/2022/discogs_20220101_releases.xml.gz`,
  `…/data/2024/discogs_20240101_releases.xml.gz`,
  `…/data/2025/discogs_20250101_releases.xml.gz`
- `https://discogs-data-dumps.s3.us-west-2.amazonaws.com/` (Listing + Einzelobjekt → 403)
- `https://api.discogs.com/releases/{1,3,5,6,7,11,13,17,66785,249504,1027881,2674825,3664623,3731745,7355025,32000000}`
- `https://api.discogs.com/masters/{1000,96559,480952}`,
  `https://api.discogs.com/artists/{1,4,46481,82730,415403,547352}`,
  `https://api.discogs.com/labels/{1,895,82835,86537}`
- `https://i.discogs.com/…` (signierte Bild-URLs; Manipulationstests)
- `https://archive.org/advancedsearch.php?q=collection:discogs-dumps&sort=publicdate+desc`

**Offizielle Discogs-Quellen** (alle für Werkzeuge Cloudflare-gesperrt, gelesen
über Wayback-Snapshots)
- API-Doku: https://www.discogs.com/developers/ ·
  Snapshot http://web.archive.org/web/20260106185457/https://www.discogs.com/developers
- Ressourcen-Doku Release/Master/Artist/Label:
  https://web.archive.org/web/20210601000000/https://www.discogs.com/developers/resources/database/release.html
- **API Terms of Use** („Last Updated: May 27th, 2025"):
  https://support.discogs.com/hc/en-us/articles/360009334593-API-Terms-of-Use ·
  Snapshot http://web.archive.org/web/20251118015920/https://support.discogs.com/hc/en-us/articles/360009334593-API-Terms-of-Use
- Terms of Service: https://support.discogs.com/hc/en-us/articles/360009334333-Terms-of-Service
- Developer Settings (Token): https://www.discogs.com/settings/developers
- Datenbank-Kennzahlen: https://www.discogs.com/about/features/discography/

**Discogs-Forum** (Cloudflare-JS-Challenge; über Suchmaschinen-Auszüge gelesen)
- S3-403 / `data.discogs.com` als Weg: https://www.discogs.com/forum/thread/1160730
- Entfernung der Bild-URLs 2015: https://www.discogs.com/forum/thread/411182
- „Data Dumps Missing URIs" (absichtlich): https://www.discogs.com/forum/thread/756269
- XML-Tags im Release-Dump: https://www.discogs.com/forum/thread/852247
- verzögerte/ausgefallene Dump-Jobs: https://www.discogs.com/forum/thread/1077473
- Prüfsummen-Dateien: https://www.discogs.com/forum/thread/346001
- Bild-Rate-Limit (Staff, 2018, alte `api-img`-Domain): https://www.discogs.com/forum/thread/709515
- aktuelle Bild-Limits (Cloudflare, ~20–30/min): https://www.discogs.com/forum/thread/997721

**Referenz-Tooling**
- discogs-xml2db (**Branch `develop`**): https://github.com/philipmat/discogs-xml2db ·
  DDL https://github.com/philipmat/discogs-xml2db/blob/develop/postgresql/sql/CreateTables.sql
- Issues #125 (FK-Verstöße), #131 (`"A1"` kein Integer), #146 (Encoding unter
  Windows), #148/#158 (Label ohne Name), #150 (Artist ohne Name),
  #151–#154 (`NOT NULL`-Verletzungen), **#155 (Bilddaten weg seit 20241201)**,
  #156 (Artist heißt `" "`), #138 (Download-URLs kaputt)
- discogs-load (Rust, DylanBartels): https://github.com/DylanBartels/discogs-load ·
  `src/release.rs`, `src/db.rs`, `sql/tables/*.sql`, `sql/indexes.sql`
- discogskit: https://github.com/jmfontaine/discogskit ·
  `src/discogskit/writers/postgresql.py`
- disco-quick: https://github.com/sublipri/disco-quick ·
  dgtools: https://github.com/marcw/dgtools ·
  discogs2pg: https://github.com/clrnd/discogs2pg
- python3-discogs-client (Backoff-Referenz):
  https://github.com/joalla/discogs_client/pull/34 ·
  https://python3-discogs-client.readthedocs.io/en/latest/optional_configuration.html#requests-rate-limiting

**Sonstiges**
- Internet-Archive-Spiegel (bis 2023-02): https://archive.org/details/discogs-dumps
- IA-Blogpost zur Bulk-Freigabe: https://blog.archive.org/2020/12/06/discogs-thank-you-a-commercial-community-site-with-bulk-data-access/
- DuckDB-Ansatz: https://www.architecture-performance.fr/ap_blog/trying-duckdb-with-discogs-data/

---

## 8. Anschluss an den HANDOFF

| HANDOFF-Stelle | Ergebnis dieser Recherche |
|---|---|
| **§4** „Dump-Spiegel, monatlich, täglicher Verfügbarkeits-Check" | bestätigt und konkretisiert: 1 HEAD/Tag auf die konstruierte `CHECKSUM.txt`-URL (§2.2, Ⓞ10); Verzug real (Jan 2026: 14 Tage) |
| **§4** „Discogs-Bilder Lazy-Cache" | technisch machbar (§4.3/4.4), aber **ToS-Konflikt** → Ⓞ8, Default bleibt aus |
| **§6.2** Referenz-Tooling prüfen | `discogs-load` **unbrauchbar**, `discogs-xml2db/develop` ist die Vorlage, `discogskit` als Beschleunigungs-Ideengeber (§1.8, Ⓞ13) |
| **§8** Schema `discogs` | konkreter Vorschlag in §1.9 (28 Tabellen + `dump_state`) |
| **§9** GET-Subset | vollständige Nachbau-Spezifikation in §5, inkl. Delta-Tabelle und wörtlicher Fehlertexte |
| **§11.3** „Stale statt Sperre", Etappen | Import-Strategie Ⓞ11 (Schattentabellen mit Switch **pro Entität**) |
| **§14.2** offene Punkte | **alle vier beantwortet**: XML-Schema (§1), Token-Rate-Limits (§4.1), Veröffentlichungsrhythmus (§2.3), Dump-URL (§2.1) |
| **§15.1** Speicherbedarf | Dumps **11,5 GiB** gz / **≈ 70 GiB** entpackt (§2.6); Postgres-Schätzung **100–180 GB** bleibt gültig, eher oberes Ende (700 Mio. Zeilen); **neu:** Download-Puffer ≥ 20 GiB, und bei Schattentabellen-Import zusätzlich der größte Einzelbestand doppelt |
| **§15.2** Risiko „Upstream-Sperren" | **konkretisiert:** `data.discogs.com` sperrt bei ~15 Anfragen/6 min für ~1 Stunde (429), **kein Resume** bei 10,4 GB (§2.5, Ⓞ9) |
