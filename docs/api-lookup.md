# API-Dienst: `/v2/lookup` (Phase 9)

Referenz zum Lookup-Endpunkt des Containers `acoustid-api`. Vertrag und
Begründungen: ARCHITECTURE §5.3 und §7 sowie
[docs/research/phase1-api-formate.md](research/phase1-api-formate.md) und
[docs/research/phase1-acoustid-index.md](research/phase1-acoustid-index.md).

**Stand:** Lookup ohne `meta`. Metadaten aus der MusicBrainz-Spiegel-Datenbank
kommen in Phase 10, `/v2/submit` in Phase 11/12, `/v2/lookup/batch` und
`/v2/submission_status` in Phase 13.

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
ab Phase 14 der Wächter als Proxy; er setzt API-Key-Prüfung, Rate-Limit und
Lookup-Cache durch — die API selbst prüft keine Keys (ARCHITECTURE §7,
„Durchsetzungsort Auth & Rate-Limit").

Env-Variablen: `AOFF_DB_*`, `AOFF_INDEX_URL`, `AOFF_INDEX_NAME`,
`AOFF_CONFIG_PATH`, `AOFF_LOG_LEVEL` (siehe `.env.example`). Aus der
`config.yaml` liest der Dienst genau einen Wert: **`index.query_hashes`**. Er
muss mit dem Wert übereinstimmen, mit dem der Importer indexiert hat — sonst
bildet die Suche einen anderen Query-Extrakt und findet nichts.

Lokal ohne Container:

```bash
AOFF_DB_HOST=127.0.0.1 AOFF_DB_PASSWORD=… AOFF_INDEX_URL=http://127.0.0.1:6081 \
  uv run python -m acoustid_api
```

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
| `meta` | nein | Wird angenommen und protokolliert, in Phase 9 **ohne Wirkung**. |

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

## Matching-Pipeline

1. **Query-Extrakt** aus dem angefragten Vollvektor
   (`shared.fpindex.extract_query`, `index.query_hashes` Hashes).
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
AOFF_DB_HOST=127.0.0.1 AOFF_INDEX_URL=http://127.0.0.1:6081 \
  uv run pytest api/tests --integration=require
```

## Bewusste Abweichungen vom Original

| Punkt | Original | Hier | Grund |
|---|---|---|---|
| Merge-Verkettung im Fingerprint-Pfad | folgt `new_id` nicht | folgt ihr bis zum Ende | Unser Bestand kommt aus den Deltas; dort bleibt `fingerprint.track_id` beim zurückgezogenen Track stehen. Ohne die Verkettung käme eine tote AcoustID heraus. |
| Indexfehler beim Lookup | wird verschluckt, Ergebnis leer | Fehler 13 / HTTP 503 | Ein leeres Ergebnis wäre von einem echten Nicht-Treffer nicht zu unterscheiden; 503 ist ohnehin das Signal des Wächters für „gleich nochmal". |
| Kandidatensuche | im `fast`-Pfad des Originals nie aufgerufen (bekannter Fehler) | immer | Ohne Indexsuche findet eine selbst gehostete Instanz nichts (Forschungsbericht, Warnung 1). |
| Rumpf `multipart/form-data` | wird gelesen | wird ignoriert | Kein bekannter Client benutzt es beim Lookup; spart eine Abhängigkeit. |
| Kaputter gzip-Rumpf | nacktes HTTP 400 ohne AcoustID-Format | gilt als leerer Rumpf, WARNING im Log, danach Fehler 2 | Die 19er-Tabelle kennt keinen Code für „kaputter Rumpf". |
| Zu großer gzip-Rumpf (`Content-Length`) | nacktes HTTP 400 | Fehler 19 / HTTP 413 | Einheitlich mit der Grenze für unkomprimierte Rümpfe; Picard wertet genau das aus. |
