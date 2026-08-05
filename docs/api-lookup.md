# API-Dienst: `/v2/lookup` und `/v2/lookup/batch` (Phasen 9, 10 und 13)

Referenz zu den beiden Lookup-Endpunkten des Containers `acoustid-api`.
Vertrag und Begründungen: ARCHITECTURE §5.3, §5.4, §6 („Batch-Limit") und §7
sowie [docs/research/phase1-api-formate.md](research/phase1-api-formate.md),
[docs/research/phase1-acoustid-index.md](research/phase1-acoustid-index.md)
und [docs/research/phase1-mb-schema.md](research/phase1-mb-schema.md).

**Stand:** Lookup **mit** `meta` (Metadaten aus der
MusicBrainz-Spiegel-Datenbank, Phase 10); seit Phase 13 zusätzlich der eigene
Endpunkt [`POST /v2/lookup/batch`](#post-v2lookupbatch-eigener-endpunkt-phase-13).
`/v2/submit` und `/v2/submission_status` stehen in
[docs/api-submit.md](api-submit.md).

## Betrieb

```bash
docker compose up -d api          # zusammen mit db und index
docker compose logs -f api
```

Der Dienst wartet auf den **Healthcheck** des Index, und der prüft
`/<name>/_health` — er wird also erst grün, wenn der Index angelegt ist. Das
macht der Importer beim Bootstrap (`ensure_index()`); vor dem ersten
Import-Lauf bleibt `api` im Zustand „waiting" (Phase-5-Vermerk in
ARCHITECTURE §5.3).

Der Dienst hat **keinen veröffentlichten Port** (`expose: 8080`). Davor sitzt
seit Phase 15 der Wächter als Reverse-Proxy für `/v2/*`; er weckt den Stack
bei Bedarf, hält seit Phase 17 den **Lookup-Cache** und setzt später
API-Key-Prüfung und Rate-Limit (Phase 18) durch — die API selbst prüft keine
Keys (ARCHITECTURE §7, „Durchsetzungsort Auth & Rate-Limit"). Antworten der
API reicht der Proxy **unverändert** durch, auch Fehlerantworten.

**Was der Cache bedeutet** (ARCHITECTURE §7 „Lookup-Cache"): Eine
Wiederholung derselben `/v2/lookup`-Anfrage bekommt die frühere Antwort
bytegleich zurück, ohne dass dieser Dienst sie noch einmal sieht — auch
dann, wenn der Stack gerade schläft. Der Schlüssel umfasst **alle**
Anfrageparameter außer `client` und `clientversion` (nur diese beiden prägen
laut Vertrag die Antwort nicht), ohne Normalisierung von Reihenfolge,
Groß-/Kleinschreibung oder Vorgabewerten. Geleert wird der Cache nach jeder
erfolgreichen lokalen Submission und nach jedem erfolgreichen Delta-Import
(Invariante §8.6) — eine `/v2/submit`-Einreichung wird also nie von einer
veralteten Lookup-Antwort verdeckt.

Env-Variablen: `MMO_DB_*`, `MMO_INDEX_URL`, `MMO_INDEX_NAME`,
`MMO_CONFIG_PATH`, `MMO_LOG_LEVEL` (siehe `.env.example`). Aus der
`config.yaml` liest der Dienst drei Werte:

| Schlüssel | Bedeutung |
|---|---|
| `acoustid.index.query_hashes` | Muss mit dem Wert übereinstimmen, mit dem der Importer indexiert hat — sonst bildet die Suche einen anderen Query-Extrakt und findet nichts. |
| `mb.dsn` | Read-only-DSN der MusicBrainz-Spiegel-Datenbank. **Leer = keine Metadaten** (der Lookup antwortet dann dauerhaft wie im degradierten Betrieb). |
| `mb.keep_submitted_mbid` | Default `false`. Bei aufgelösten Recording-Redirects trägt die Antwort die **kanonische** MBID; `true` reicht stattdessen die eingereichte durch. |

Lokal ohne Container:

```bash
MMO_DB_HOST=127.0.0.1 MMO_DB_PASSWORD=… MMO_INDEX_URL=http://127.0.0.1:6081 \
  uv run python -m acoustid_api
```

### Interner Healthcheck `GET /_health` (Phase 15)

**Kein Teil des API-Vertrags** (ARCHITECTURE §7) und bewusst nicht unter
`/v2/`: der Endpunkt existiert allein als **Bereitschaftsprüfung des
Wächters** (DECISIONS 2026-08-01). Er ist nur im Compose-Netz erreichbar —
der Dienst veröffentlicht keinen Port, und der Proxy des Wächters reicht
ausschließlich `/v2/*` weiter. Clients dürfen sich nicht darauf verlassen;
er kann sich ohne Vertragsbruch ändern.

Er prüft genau zwei Anbindungen, beide leichtgewichtig (der Wächter fragt
im Sekundentakt, solange er weckt):

| Prüfung | Womit | Warum |
|---|---|---|
| `db` | `SELECT 1` auf einer Pool-Verbindung | Prüft die Kette bis in die Postgres, ohne eine Tabelle anzufassen. Nach einem Kaltstart antwortet uvicorn lange vor dem Ende der Recovery. |
| `index` | `GET /<name>/_health` des acoustid-index | Sagt, ob der Index existiert und geladen ist (`404` = fehlt, `503` = lädt noch). Vor dem ersten Bootstrap-Lauf gibt es ihn nicht. |

**Nicht geprüft wird MusicBrainz**: der Spiegel darf fehlen (Invariante
§8.7, degradierter Betrieb). Wäre er Teil der Bereitschaft, würde ein
MB-Ausfall den ganzen Stack als „nicht bereit" abstempeln.

```json
// HTTP 200
{"status": "ok", "version": "0.0.1", "checks": {"db": "ok", "index": "ok"}}
// HTTP 503
{"status": "error", "version": "0.0.1",
 "checks": {"db": "ok", "index": "Index 'main' fehlt oder lädt noch"}}
```

Die Antwort trägt **nicht** das AcoustID-Fehlerformat — dessen 19 Codes
passen auf keinen der Fälle. Auch ein Fehler in der Prüfung selbst wird zu
`503`: für den Wächter bedeuten alle Misserfolge dasselbe.

Derselbe Endpunkt ist seit Phase 15 der **Container-Healthcheck** des
Dienstes (`docker-compose.yml`), damit `docker ps` etwas Aussagekräftiges
zeigt. Von außen erreichbar wird er dadurch nicht.

### Reload der `config.yaml` im laufenden Betrieb (Phase 15)

Der Wächter ist der einzige Schreiber der `config.yaml` und legt nach jedem
Speichern die Marke `config.yaml.reload` daneben (JSON, monoton wachsender
Zähler `generation`). Der API-Dienst liest sie beim Start und danach alle
10 Sekunden; bei neuer Nummer lädt er die Datei neu. Kein Neustart nötig,
kein offener Port — die Marke läuft über denselben read-only-Mount wie die
Konfiguration.

Übernommen wird **nur, was der Dienst zur Anfragezeit liest**:

| Schlüssel | Wirkt sofort? |
|---|---|
| `acoustid.submit.mode`, `acoustid.submit.upstream_app_key` | **Ja** — der Upstream-Weiterleiter wird dabei neu gebaut (er entsteht sonst nur beim Start und fehlte nach einem Wechsel auf `local+upstream`). |
| `mb.keep_submitted_mbid` | **Ja** (wird je Anfrage gelesen). |
| `acoustid.index.query_hashes` | **Nein** — eine Änderung verlangt einen Index-Neuaufbau (§6). Der laufende Wert bleibt stehen, die Abweichung wird als Warnung geloggt. |
| `mb.dsn` | **Nein** — Pool und Schema-Selfcheck entstehen beim Start; Änderung erst nach Neustart des Containers. |

Die beiden „Nein"-Fälle werden nicht halb übernommen: der laufende Wert
wird in die neue Konfiguration zurückgeschrieben, damit `service.config`
immer beschreibt, was der Prozess tatsächlich tut. Eine ungültige Datei
lässt die alte Konfiguration in Kraft (Grund im Log).

## Parameter

| Name | Pflicht | Bedeutung |
|---|---|---|
| `client` | ja | Application-Key. Wird nur auf Anwesenheit geprüft (Prüfung macht der Wächter). |
| `fingerprint` | ja¹ | Komprimierter Chromaprint, URL-sicheres Base64 ohne Padding, nur Version 1. |
| `duration` | ja¹ | Länge der Aufnahme in Sekunden. `0` und unlesbare Werte gelten als fehlend. |
| `trackid` | ja¹ | Statt Fingerprint: AcoustID direkt nachschlagen (Score 1.0). |
| `maxdurationdiff` | nein | Längentoleranz in Sekunden, 1…30, Default 7. |
| `batch` | nein | ≠ 0 ⇒ alle Teilanfragen beantworten (Antwortform `fingerprints[]`). |
| `format` | nein | `json` (Default), `jsonp`, `xml`. |
| `jsoncallback` | nein | Funktionsname für `jsonp`, Default `jsonAcoustidApi`. |
| `clientversion` | nein | Nur fürs Log. |
| `meta` | nein | Welche Metadaten die Antwort trägt — siehe unten. |

¹ Je Teilanfrage entweder `trackid` **oder** `fingerprint` + `duration`.

**Transport:** `GET` und `POST` gleichwertig; Parameter aus Query-String
**und** `application/x-www-form-urlencoded`-Rumpf (Query-String gewinnt bei
Namensgleichheit); `Content-Encoding: gzip` wird entpackt; Rumpfgrenze 1 MiB
vor und nach dem Entpacken; jede Antwort trägt
`Access-Control-Allow-Origin: *`.

**Original-Batchprotokoll:** `fingerprint.N`/`duration.N`/`trackid.N` mit
`batch=1`. Ohne `batch` wird nur die **erste** Teilanfrage beantwortet.
Grenzen: 20 Fingerprint-, 100 Track-Teilanfragen (sonst Fehler 19/413). Eine
unlesbare Teilanfrage wird still übersprungen; ein Fehler kommt nur, wenn
keine gültige übrig bleibt.

## Antwort

```json
{"status": "ok", "results": [{"id": "<acoustid>", "score": 0.987}]}
```

Mit `batch`:

```json
{"status": "ok", "fingerprints": [{"index": 0, "results": [...]}]}
```

`index` ist die Nummer aus dem Parameter-Suffix bzw. `null`. Fehler:

```json
{"status": "error", "error": {"code": 19, "message": "request too large"}}
```

19 Codes mit festem HTTP-Status (`api/app/errors.py`); abweichend von 400
sind 5→500, 13→503, 14→429, 18→404, 19→413.

## `meta` — Metadaten in der Antwort

Der Wert wird an **Whitespace** zerlegt; zusätzlich gibt es die numerische
Kurzform `0` (nichts), `1` (= `recordingids`) und `2` (= `m2`). Unbekannte
Werte werden stillschweigend ignoriert.

| Wert | Wirkung |
|---|---|
| `recordings` | Aufnahmen mit `id`, `title` (immer, ggf. `""`), `duration` (nur wenn gesetzt) und `artists[]`. |
| `recordingids` | Nur die Recording-MBID. Spart die Nutzdaten: MusicBrainz wird nur nach der Existenz gefragt. |
| `releases` | Veröffentlichungen mit `id`, `title`, `medium_count`, `track_count`, `artists[]`, `releaseevents[]`. |
| `releaseids` | Nur die Release-MBID. |
| `releasegroups` | Release-Gruppen mit `id`, `title`, `type`, `secondarytypes[]`, `artists[]`. |
| `releasegroupids` | Nur die Release-Gruppen-MBID. |
| `tracks` | Je Veröffentlichung `mediums[]` mit `position`, `track_count`, `format`, `title` und `tracks[]`. |
| `compress` | Löscht Felder, die dem übergeordneten Objekt entsprechen (siehe unten). |
| `sources` | `submission_count` je MBID — aus **unserer** Datenbank, nicht aus MusicBrainz. Picard gewichtet damit sein Ranking. |
| `usermeta` | Rückfall auf eingereichte Textmetadaten (`meta`/`track_meta`), **nur** wenn MusicBrainz zu keiner MBID etwas liefert. |
| `m2` | Ältere Antwortform: Aufnahme mit flacher `tracks[]`-Liste, jeder Track mit seinem `medium` und dessen `release`. |

**Präzedenz — genau ein Zweig.** Welcher Schlüssel unmittelbar unter einem
Treffer erscheint, entscheidet diese Reihenfolge; der erste zutreffende
gewinnt, alle weiteren wirken nur noch als Detailgrad:

1. `m2`
2. `recordings` | `recordingids`
3. `releasegroups` | `releasegroupids`
4. `releases` | `releaseids`

`meta=recordings releasegroups releases tracks compress sources` (Picard)
ergibt deshalb `results[] → recordings[] → releasegroups[] → releases[] →
mediums[] → tracks[]`; `meta=releases` allein hängt `releases[]` direkt unter
den Treffer — dann ohne MBID der Aufnahme.

Beispiel (gekürzt):

```json
{"status": "ok", "results": [{
  "id": "<acoustid>", "score": 0.98,
  "recordings": [{
    "id": "<recording-mbid>", "title": "Titel", "duration": 209.0, "sources": 7,
    "artists": [{"id": "<artist-mbid>", "name": "Band", "joinphrase": " feat. "}],
    "releasegroups": [{
      "id": "<rg-mbid>", "title": "Album", "type": "Album",
      "secondarytypes": ["Compilation"],
      "releases": [{
        "id": "<release-mbid>", "medium_count": 2, "track_count": 22,
        "country": "DE", "date": {"year": 1999, "month": 7},
        "releaseevents": [{"country": "DE", "date": {"year": 1999, "month": 7}}],
        "mediums": [{"position": 1, "track_count": 12, "format": "CD",
                     "tracks": [{"id": "<track-mbid>", "position": 4}]}]
      }]
    }]
  }]
}]}
```

Eigenheiten des Originals, die hier bewusst nachgebildet sind:

- **Sekunden werden abgeschnitten, nie gerundet** (`length / 1000` als
  Ganzzahldivision): 209 999 ms sind `209`, nicht `210`. Serialisiert wird
  als Fließkommazahl (`209.0`).
- **Das erste Release-Ereignis steht zusätzlich flach im Release** (`country`
  und `date` neben `releaseevents[]`).
- `track_count` eines Mediums zählt Data-Tracks mit.
- `compress` löscht: Track-Titel gleich dem Titel der Aufnahme, Track-Künstler
  gleich denen der Veröffentlichung, Release-Künstler/-Titel gleich denen der
  Release-Gruppe — und die Künstler der Release-Gruppe **nur bei der letzten**
  Gruppe einer Aufnahme (eine Einrückung im Original, Wert für Wert
  nachgebildet).
- `usermeta`-Künstler stehen als nackte Zeichenketten in `artists[]`, nicht
  als `{"id": …, "name": …}`.

## `POST /v2/lookup/batch` (eigener Endpunkt, Phase 13)

Der einzige eigene Endpunkt der API (ARCHITECTURE §7 „Eigene Endpoints") —
api.acoustid.org kennt ihn nicht. Es gibt hier also **kein Original**, das
bug-für-bug nachzubauen wäre; maßgeblich ist der eigene Lookup-Vertrag: in
jedem Eintrag gelten dieselbe Grammatik, dieselbe Prüfreihenfolge und
dieselbe Fehlertabelle.

**Wozu.** Diese Instanz schläft im Normalfall. Wer 300 Dateien taggt, weckt
sie über das Original-Batchprotokoll (max. 20 Fingerprints je Anfrage)
fünfzehnmal — über diesen Endpunkt dreimal. Eine Anfrage, ein Weckvorgang,
ein Bündel MusicBrainz-Abfragen.

### Anfrage

`POST` (kein `GET` — der Endpunkt lebt von seinem Rumpf).
`Content-Type: application/json`; `Content-Encoding: gzip` wird entpackt;
Rumpfgrenze 1 MiB vor und nach dem Entpacken (darüber Fehler 19 / HTTP 413).

```json
{
  "client": "…",
  "meta": "recordings sources",
  "queries": [
    {"fingerprint": "AQABz…", "duration": 241},
    {"fingerprint": "AQADt…", "duration": 180, "maxdurationdiff": 30},
    {"trackid": "b81f83ee-4da4-11e0-9ed8-0025225356f3", "meta": "releases"}
  ]
}
```

Feld der Hülle:

| Name | Pflicht | Bedeutung |
|---|---|---|
| `queries` | ja | Array der Einträge, **höchstens 100** (ARCHITECTURE §6). Leeres Array ist erlaubt. |
| `client` | ja | Application-Key; nur auf Anwesenheit geprüft. Darf stattdessen im Query-String stehen — dann gewinnt der Query-String. |
| `meta` | nein | Vorgabewert für alle Einträge. |
| `maxdurationdiff` | nein | Vorgabewert für alle Einträge, 1…30 (Default 7). |
| `clientversion` | nein | Nur fürs Log; ebenfalls aus dem Query-String lesbar. |

Je Eintrag:

| Name | Pflicht | Bedeutung |
|---|---|---|
| `fingerprint` | ja¹ | Komprimierter Chromaprint, wie beim Lookup. |
| `duration` | ja¹ | Länge in Sekunden; Zahl **oder** Zahl als Zeichenkette. |
| `trackid` | ja¹ | Statt Fingerprint: AcoustID direkt nachschlagen (Score 1.0). |
| `meta` | nein | Überschreibt den Wert der Hülle. Zeichenkette wie im Original (`"recordings sources"`, auch die Kurzform `0`/`1`/`2`) **oder** JSON-Array (`["recordings", "sources"]`). |
| `maxdurationdiff` | nein | Überschreibt den Wert der Hülle. |

¹ Je Eintrag entweder `trackid` **oder** `fingerprint` + `duration`.

`true`/`false`, `null`, Objekte und Arrays sind an Stellen, wo ein einzelner
Wert erwartet wird, kein Wert — sie gelten als „nicht angegeben" und führen
in dieselbe Meldung wie ein fehlender Parameter.

### Antwort

Immer HTTP 200 und immer JSON, sofern die **Anfrage** in Ordnung war. Das
Array steht in Anfragereihenfolge; jeder Eintrag ist eine vollständige
AcoustID-Antwort und trägt zusätzlich seine Position als `index`:

```json
{"status": "ok", "responses": [
  {"index": 0, "status": "ok", "results": [{"id": "<acoustid>", "score": 0.98}]},
  {"index": 1, "status": "error", "error": {"code": 3, "message": "invalid fingerprint"}}
]}
```

**Teilfehler reißen die anderen Einträge nicht** (DoD Phase 13). Alles, was
ein Eintrag selbst falsch machen kann, wird zu seinem eigenen Fehlerobjekt;
die Gesamtantwort bleibt HTTP 200. Ein anderer Status würde Clients die ganze
Antwort verwerfen lassen — und Picard-artige Clients zum erneuten Senden
bringen.

Was dagegen der **Anfrage** fehlt, beendet sie ganz, im gewohnten
Fehlerformat mit dem gewohnten HTTP-Status:

| Lage | Antwort |
|---|---|
| `client` fehlt | 2 / 400 |
| Rumpf ist kein Objekt, `queries` fehlt oder ist keine Liste, Rumpf unlesbar/leer | 2 / 400 (`missing required parameter "queries"`) |
| `maxdurationdiff` der Hülle außerhalb 1…30 | 11 / 400 |
| mehr als 100 Einträge | 19 / 413 |
| Rumpf > 1 MiB (auch entpackt) | 19 / 413 |
| Suchindex antwortet nicht | 13 / 503 |
| MusicBrainz-Abfrage scheitert trotz stehender Verbindung | 5 / 500 |

Die letzten beiden sind Absicht: **gemeinsame Betriebsmittel gehören der
Anfrage, nicht einem Eintrag.** Fällt der Index aus, kann kein einziger
Eintrag beantwortet werden; hundert Einträge mit Code 13 wären dieselbe
Information in schlechter Verpackung, und der 503 ist zugleich das Signal, auf
das Clients und der Wächter reagieren.

### `meta` im Batch — ein Bündel statt hundert

Die Einträge werden nach ihrem ausgewerteten `meta`-Plan gruppiert; je Plan
läuft die Metadaten-Auflösung **einmal** über die Trefferobjekte aller
Einträge dieser Gruppe. Schicken alle Einträge dasselbe `meta` — der
Normalfall —, kostet die ganze Anfrage genau ein Bündel MB-Abfragen. Dieselbe
AcoustID in mehreren Einträgen kostet nichts extra: ihre Trefferobjekte
teilen sich einen Eintrag in der Zuordnung `track_id → Objekte`, genau wie im
Original-Batchprotokoll.

### Entscheidungen und Abweichungen

| Punkt | Festlegung | Grund |
|---|---|---|
| Rumpfform | Objekt `{"queries": [...]}`, **kein** nacktes Array | ARCHITECTURE §7 spricht von einem Array; die Hülle trägt dasselbe Array und lässt zusätzlich anfrageweite Felder zu (`client`, `meta`, `maxdurationdiff` — und künftige, ohne den Vertrag zu brechen). Ein nacktes Array antwortet mit Fehler 2 und nennt das fehlende Feld. |
| Antwortform | `responses[]`, je Eintrag eine vollständige AcoustID-Antwort (`status` + `results`/`error`) | Der Client kann jeden Eintrag mit demselben Code auswerten wie eine Einzelantwort. Ein gemischtes Array aus Trefferlisten und Fehlerobjekten ohne `status` wäre nicht unterscheidbar. |
| `index` je Eintrag | Position im Anfrage-Array (0-basiert), immer gesetzt | Die Reihenfolge ist zugesichert; der Index macht sie nachprüfbar und erlaubt Zuordnung auch nach Umsortieren im Client. **Nicht** zu verwechseln mit dem `index` des Original-Batchprotokolls (dort die `.N`-Suffixnummer, `null` erlaubt). |
| HTTP-Status bei Teilfehlern | 200 | Siehe oben. |
| Grenze 100 | Fehler **19** / HTTP 413 | Derselbe Code wie bei zu vielen Teilanfragen im Lookup und bei zu großem Rumpf; Picard verkleinert genau darauf seine Pakete. Die beiden Grenzen widersprechen sich nicht: echte Fingerprints aus dem Tages-Delta sind base64 im Median 3,5 KB groß (p95 3,9 KB), 100 Einträge ergeben also rund 350 KiB. Erst bei ungewöhnlich langen Aufnahmen (Ausreißer bis ~9,5 KB) kann stattdessen die Rumpfgrenze zuerst greifen — mit demselben Code. `Content-Encoding: gzip` verschafft zusätzlich Luft. |
| Leeres `queries` | HTTP 200, `"responses": []` | Eine wohlgeformte Anfrage ohne Arbeit. Ein Fehler würde einen Client bestrafen, der seine eigene Liste leer gefiltert hat. |
| `format` | **wird nicht ausgewertet**; die Antwort ist immer JSON | Ein JSON-Endpunkt, der auf Wunsch XML antwortet, wäre ein zweiter Vertrag ohne Abnehmer; die gemischte ok/error-Liste hätte in XML keinen sinnvollen Elementnamen, und `jsonp` ist ein Browser-GET-Behelf. Auch `format=xml` bleibt hier folgenlos (kein Fehler 1). |
| `client` | Pflicht, nur auf Anwesenheit geprüft | Wie beim Lookup; die Key-Prüfung macht der Wächter (§7). |
| Methode | nur `POST`; `GET` ⇒ HTTP 405 | Ein `GET` mit Pflicht-Rumpf ist kein Vertrag. Der 405 kommt von FastAPI und trägt **nicht** das AcoustID-Fehlerformat — die einzige Antwort der API, für die das gilt. |
| Unlesbarer JSON-Rumpf | gilt als leerer Rumpf, `WARNING` im Log, danach Fehler 2 | Dieselbe Regel wie beim kaputten gzip-Rumpf (Phase 9): die 19er-Tabelle kennt keinen Code für „kaputter Rumpf". |
| `Content-Type` | wird **nicht** erzwungen | Der Rumpf ist der Vertrag, nicht sein Etikett. (An den Formular-Routen wird weiterhin gefiltert — dort gibt der Query-String der Anfrage noch Sinn.) |
| Dubletten im Batch | werden einzeln gesucht, nicht zusammengefasst | Die Suche ist billig gegenüber dem Weckvorgang; `meta` wird ohnehin gebündelt. Ein Zusammenfassen würde die Reihenfolge-Zusicherung verkomplizieren, ohne einen realen Fall zu bedienen. |

## Anbindung der MusicBrainz-Datenbank

Der Zugriff läuft über einen **eigenen kleinen Pool** (`shared/shared/mb/`),
getrennt vom Lookup-Pool: der Spiegel gehört nicht zu diesem Stack und darf
eine Anfrage nicht mit in seine Wartezeit ziehen.

| Einstellung | Wert | Warum |
|---|---|---|
| `connect_timeout` | 2 s | Länger warten lohnt nicht — wir degradieren ohnehin. |
| `statement_timeout` | 2000 ms | Serverseitige Frist je Anweisung. |
| `default_transaction_read_only` | `on` | Gürtel zum Hosenträger der Rolle `acoustid_ro`. |
| `idle_in_transaction_session_timeout` | 5000 ms | Notbremse gegen hängende Transaktionen auf fremder Datenbank. |
| `search_path` | `musicbrainz, public` | Alle Abfragen sind trotzdem schema-qualifiziert. |
| Pool | max. 4 Verbindungen, 2 s Wartezeit, Pre-Ping | Eine Privatinstanz braucht nicht mehr. |
| Circuit-Breaker | 3 Fehler in 30 s → 30 s Sperre | Dokumentierte Konstanten, **kein** Config-Schlüssel. |
| Zeilenobergrenze | 5000 Zeilen je Release-Abfrage | DoS-Schutz; greift sie, wird gekürzt **und** geloggt. |

Alle Abfragen einer Anfrage laufen in **einer** Read-only-Transaktion (ein
Snapshot). Genau **eine** Datei kennt MusicBrainz-Tabellennamen
(`shared/shared/mb/queries.py`); ein Test hält die Regel fest.

**Beim Start** liest der Dienst `replication_control` und vergleicht die
Spalten der 17 erwarteten Relationen mit dem Systemkatalog:

- Fehlende Spalten ⇒ lautes `ERROR` im Log, Start trotzdem, Lookups
  antworten ohne Metadaten. Zusätzliche Spalten sind **kein** Mismatch.
- Fehlt die View `release_event`, wird auf `release_country UNION
  release_unknown_country` zurückgefallen (`WARNING`).
- Abweichende Schema-Sequenz (erwartet: 31) ⇒ `WARNING`, kein Fehler.
- Replikationsalter > 36 h ⇒ `WARNING`, > 7 Tage ⇒ `ERROR`. Ausgeliefert
  werden die Daten weiterhin.

Die Read-only-Rolle legt der Betreiber einmalig selbst an (SQL-Schnipsel in
[docs/research/phase1-mb-schema.md](research/phase1-mb-schema.md)); nach
einem MB-Schema-Upgrade muss `GRANT SELECT` erneuert werden.

### Degradierter Betrieb (Invariante §8.7)

| Lage | Antwort |
|---|---|
| `mb.dsn` leer, Spiegel nicht erreichbar, Circuit-Breaker offen, Schema-Mismatch | **HTTP 200.** AcoustID-UUIDs bleiben; in den Zweigen `m2`/`recordings`/`recordingids` auch die MBIDs und `sources` (beide aus der eigenen Datenbank). Ereignis im Log. |
| Abfrage scheitert trotz stehender Verbindung und passendem Schema | Fehler 5 / **HTTP 500**. Bewusst kein degradierter Betrieb — sonst verschwindet ein Programmfehler hinter leeren Metadaten. |

In den Zweigen `releases`/`releasegroups` bleibt im degradierten Betrieb eine
leere Liste übrig; MBIDs trägt diese Antwortform auch im Normalfall nicht.
Beide bekannten Clients (Picard, beets) schicken `recordings` mit und sehen
die MBIDs deshalb auch bei ausgefallenem Spiegel.

## Matching-Pipeline

1. **Query-Extrakt** aus dem angefragten Vollvektor
   (`shared.fpindex.extract_query`, `acoustid.index.query_hashes` Hashes).
2. **Kandidaten** vom acoustid-index: `POST /:index/_search`, `limit` 40,
   `timeout` 2000 ms.
3. **Längenfilter** in Postgres: `length BETWEEN duration ± maxdurationdiff`;
   Zeilen ohne Vektor, ohne Track oder ohne Länge fallen heraus.
4. **Rescoring** je Kandidat mit `shared.fingerprint.compare2`
   (`max_offset` 80) gegen den Vollvektor aus Postgres.
5. **Auswahl:** Score > 0,4; Sortierung Score absteigend, bei Gleichstand
   `fingerprint.id` aufsteigend; **Kappung auf 10 Zeilen, danach**
   Deduplizierung je Track (Reihenfolge wie im Original).
6. **Track-Auflösung:** `fingerprint.track_id` → `track.gid`, dabei wird die
   Merge-Verkettung über `track.new_id` bis zum Ende verfolgt (max. 10
   Glieder).

Antwortet der Index nicht (Netz, Ladevorgang, Suchfrist), gibt es Fehler 13 /
HTTP 503 — **keine** leere Trefferliste.

## Bit-Verifikation des Rescorings

`extract_query` und `compare2` sind Nachbauten der C-Extension pg_acoustid,
die produktiv bewusst nicht eingesetzt wird (DECISIONS 2026-07-25). Ein
eigener Test-Container hält die Original-Extension als Referenz; der Vergleich
läuft ohne Toleranz, Wert für Wert:

```bash
docker build -t acoustid-offline-pg-acoustid:test tests/pg_acoustid
docker run -d --rm --name pg-acoustid -p 127.0.0.1:5443:5432 \
  -e POSTGRES_PASSWORD=test acoustid-offline-pg-acoustid:test
ACOUSTID_EXTENSION_DSN=postgresql://postgres:test@127.0.0.1:5443/postgres \
  uv run pytest -m extension --integration=require
```

In CI ist das der eigene Job „Bit-Verifikation des Rescorings (pg_acoustid)";
das Image wird nie veröffentlicht (pg_acoustid hat keine Lizenzdatei).

## Tests

```bash
uv run pytest api/tests                       # HTTP-Schicht, ohne Dienste

docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db index
MMO_DB_HOST=127.0.0.1 MMO_INDEX_URL=http://127.0.0.1:6081 \
  uv run pytest api/tests --integration=require
```

Der Batch-Endpunkt liegt in `api/tests/test_batch_http.py` (ohne Dienste) und
im Abschnitt „Eigener Batch-Endpunkt" von
`api/tests/test_lookup_integration.py` — dort auch der Nachweis, dass **eine**
Anfrage sowohl Delta-Bestand als auch lokale Einreichungen findet (beide
Dokument-ID-Bereiche, ARCHITECTURE §5.3).

Die `meta`-Integrationstests brauchen **nur Postgres**: sie schlagen über
`trackid` nach und fassen den Suchindex nicht an. Das MusicBrainz-Schema
entsteht dafür als **Mini-Fixture** (`api/tests/mb_fixture.py`) im Schema
`musicbrainz` derselben Wegwerf-Datenbank — 17 Relationen, die
`release_event`-View und eine Handvoll synthetischer Zeilen. Echte
MusicBrainz-Dumps kommen in den Tests bewusst nicht vor.

## Bewusste Abweichungen vom Original

| Punkt | Original | Hier | Grund |
|---|---|---|---|
| Merge-Verkettung im Fingerprint-Pfad | folgt `new_id` nicht | folgt ihr bis zum Ende | Unser Bestand kommt aus den Deltas; dort bleibt `fingerprint.track_id` beim zurückgezogenen Track stehen. Ohne die Verkettung käme eine tote AcoustID heraus. |
| Indexfehler beim Lookup | wird verschluckt, Ergebnis leer | Fehler 13 / HTTP 503 | Ein leeres Ergebnis wäre von einem echten Nicht-Treffer nicht zu unterscheiden; 503 ist ohnehin das Signal des Wächters für „gleich nochmal". |
| Kandidatensuche | im `fast`-Pfad des Originals nie aufgerufen (bekannter Fehler) | immer | Ohne Indexsuche findet eine selbst gehostete Instanz nichts (Forschungsbericht, Warnung 1). |
| Rumpf `multipart/form-data` | wird gelesen | wird ignoriert | Kein bekannter Client benutzt es beim Lookup; spart eine Abhängigkeit. |
| Kaputter gzip-Rumpf | nacktes HTTP 400 ohne AcoustID-Format | gilt als leerer Rumpf, WARNING im Log, danach Fehler 2 | Die 19er-Tabelle kennt keinen Code für „kaputter Rumpf". |
| Zu großer gzip-Rumpf (`Content-Length`) | nacktes HTTP 400 | Fehler 19 / HTTP 413 | Einheitlich mit der Grenze für unkomprimierte Rümpfe; Picard wertet genau das aus. |

### Zusätzlich ab `meta` (Phase 10)

| Punkt | Original | Hier | Grund |
|---|---|---|---|
| Aufnahme ohne Veröffentlichung bei `meta=…releases` | verschwindet vollständig (INNER JOIN) — auch Titel, Länge und Künstler | Basisdaten bleiben, `releases` ist leer | Der Verlust betrifft ausgerechnet die Felder, die der Client sicher braucht (Forschungsbericht, Fallstrick 1). |
| MBID, die MusicBrainz nicht (mehr) kennt | wird als Merge-Auftrag in eine Warteschlange gestellt, nach 7 Tagen deaktiviert; `resolve_mbid_redirect` ist toter Code | Auflösung **online** gegen `recording_gid_redirect`, Antwort mit kanonischer MBID (`mb.keep_submitted_mbid` kehrt das um) | Ohne Auflösung lieferte die Instanz für jede zusammengeführte Aufnahme dauerhaft leere Metadaten — der realistische Haupt-Fehlerfall (DECISIONS 2026-07-25). |
| MusicBrainz nicht erreichbar | HTTP 500 | HTTP 200 ohne Metadaten, Ereignis im Log | Invariante §8.7: eine Antwort mit UUIDs und MBIDs ist brauchbar, ein 500 nicht. |
| Fehlende Spalte / verweigerte Rechte auf dem Spiegel | HTTP 500 | HTTP 200 ohne Metadaten, `ERROR` im Log | Dasselbe Argument; das jährliche MB-Schema-Update darf die Instanz nicht abschalten. |
| Zeilenzahl der Release-Abfrage | unbegrenzt | 5000 Zeilen, danach gekappt + `WARNING` | Eine Anfrage darf 20 Fingerprints × N MBIDs × jede Veröffentlichung ziehen — ein DoS-Vektor. |
| Reihenfolge der Release-Ereignisse und Sekundärtypen | ungeordnet (Planer entscheidet) | deterministisch (Land/Datum bzw. `child_order`) | Das **erste** Ereignis wird flach ins Release kopiert; eine zufällige Reihenfolge machte die Antwort unreproduzierbar. |
| `m2` mit einem Track ohne Länge | `float(None)` ⇒ HTTP 500 | Feld `duration` fehlt | Tracks ohne Länge sind in MusicBrainz normal. |
| `m2` mit demselben Treffer in mehreren Teilanfragen | teilt eine `tracks`-Liste (Aliasing) und hängt Tracks mehrfach an | jedes Trefferobjekt bekommt seine eigene Liste | Der Effekt wäre eine duplizierte Trackliste, kein Kompatibilitätsmerkmal. |
| Fehlende Nebenzeilen (Artist-Credit, Medienzahl, Release-Gruppe) | `KeyError` ⇒ HTTP 500 | Feld fehlt bzw. leere Liste | Fallstrick 3 des Forschungsberichts. |
| `compress` bei leerer `releasegroups`-Liste | `NameError` ⇒ HTTP 500 | ohne Wirkung | Ein Absturz ist kein Verhalten, das ein Client auswerten könnte. |
