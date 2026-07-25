# Phase-1-Recherche: AcoustID Web-API v2 — exakte Formate (2026-07-25)

Recherche-Report der Implementierungs-Session (Opus-Agent, von Fable
synthetisiert). Primärquellen: https://acoustid.org/webservice
(Quelltext `acoustid/web/pages/webservice.md`) und
`github.com/acoustid/acoustid-server` @ `acae927` (v26.3.1, 2026-03-15).
Keine Requests gegen api.acoustid.org gesendet.

## Transport-Grundlagen (alle v2-Endpunkte)

- GET **und** POST; Parameter aus Query-String **+** Form-Body
  (`req.values`). Body nur `application/x-www-form-urlencoded`/
  multipart — **kein JSON-Body**.
- `Content-Encoding: gzip` auf Request-Bodys wird entpackt
  (WSGI-Middleware).
- Limits: `MAX_CONTENT_LENGTH = 1 MiB`, 1000 Form-Parts; Überschreitung
  ⇒ Fehlercode 19 / HTTP 413. **Picard stützt sein Batch-Sizing auf
  dieses 413.**
- CORS: `Access-Control-Allow-Origin: *` auf allen Antworten.
- Kein HTTPS-Zwang für lookup/submit. Doppelte Slashes im Pfad werden
  normalisiert.
- Antwort-Content-Types: `application/json; charset=UTF-8`,
  `application/javascript` (jsonp), `text/xml`. JSON mit
  `sort_keys=True`.
- Routen: `/v2/lookup`, `/v2/submit`, **`/v2/submission_status`**
  (NICHT `/v2/submit/status` — Handoff-Korrektur!), `/v2/fingerprint`,
  `/v2/track/list_by_mbid`, `/v2/track/list_by_puid`, `/v2/user/*`,
  Legacy v1 `/lookup`, `/submit` (XML).

## /v2/lookup

Parameter (dokumentiert): `format` (json Default | jsonp | xml),
`jsoncallback` (Default `jsonAcoustidApi`), `client` (Pflicht),
`duration` + `fingerprint` ODER `trackid`, `meta`.
Zusätzlich im Code: `batch` (≠0 ⇒ `fingerprints[]`-Antwortformat, alle
Teilanfragen werden verarbeitet; ohne `batch` nur die erste!),
`maxdurationdiff` (1…30, Default 7, sonst Fehler 11), `clientversion`,
Suffix-Parameter `fingerprint.N`/`duration.N`/`trackid.N`.

Limits: max **20** Fingerprint-Queries, max **100** Track-Queries pro
Request (sonst 19/413); max **10** Ergebnisse je Query; nur Treffer mit
**score > 0.4**; absteigend sortiert; Track-IDs dedupliziert. Im Batch
werden unparsbare Teilanfragen still übersprungen (Fehler nur, wenn
keine gültig ist).

`meta`-Werte (Split an **Whitespace**; numerisch: 0=keine,
1=recordingids, 2=m2): `recordings`, `recordingids`, `releases`,
`releaseids`, `releasegroups`, `releasegroupids`, `tracks`, `compress`,
`usermeta`, `sources`, `m2`. **Präzedenz (genau ein Zweig):** m2 >
(recordings|recordingids) > (releasegroups|releasegroupids) >
(releases|releaseids). `sources` = `track_mbid.submission_count` (aus
eigener DB, keine MB-Query); `usermeta` = Fallback auf eigene
meta-Tabelle, nur wenn MB leer; `tracks`/`compress` = reine
Strukturmodifikatoren.

Fingerprint-Encoding: Chromaprint-Base64 — Alphabet
`A–Za–z0–9-_`, **URL-safe, kein Padding**; nur `FINGERPRINT_VERSION=1`,
sonst Fehler 3. Nicht Pythons Standard-b64decode verwenden.

Antwort (Erfolg):
```json
{"status": "ok", "results": [{"id": "<track-gid>", "score": 1.0}]}
```
Batch: `{"status":"ok","fingerprints":[{"index":0,"results":[…]}]}`
(`index` int oder null). Feldinventar: recording `id`,`title` (immer,
ggf. ""),`duration` (nur wenn gesetzt),`artists[]`, opt. `sources`;
artist `id`,`name`, opt. `joinphrase`; releasegroup `id`,`title`,
`type`,`secondarytypes[]`,`artists[]`,`releases[]`; release `id`,
`title`,`medium_count`,`track_count`,`artists[]`,`releaseevents[]`
(je `country`,`date{year,month,day}`) — **Felder des ersten Events
werden zusätzlich flach ins Release kopiert**; medium `position`,
`track_count`,`format`,`title`,`tracks[]`; track `id`,`position`,
`title`,`artists[]`.

### Fehlerformat

```json
{"status": "error", "error": {"code": 4, "message": "invalid API key"}}
```

| Code | HTTP | Message |
|---|---|---|
| 1 | 400 | unknown format "<name>" |
| 2 | 400 | missing required parameter "<name>" |
| 3 | 400 | invalid fingerprint |
| 4 | 400 | invalid API key |
| 5 | **500** | internal error |
| 6 | 400 | invalid user API key … |
| 7 | 400 | parameter "<name>" is not a valid UUID |
| 8 | 400 | parameter "<name>" must be a positive integer |
| 9 | 400 | parameter "<name>" must be a positive integer |
| 10 | 400 | … not a valid foreign ID, … format vendor:id |
| 11 | 400 | parameter "<name>" must be between 1 and 30 |
| 12 | 400 | not allowed |
| 13 | **503** | service currently unavailable, try again later |
| 14 | **429** | rate limit (%f requests per second) exceeded … |
| 15 | 400 | invalid MusicBrainz access token |
| 16 | 400 | only requests over HTTPS are allowed here |
| 17 | 400 | unknown application |
| 18 | **404** | fingerprint not found |
| 19 | **413** | request too large |

Kein `Retry-After`-Header, keine `X-RateLimit`-Header. Ungültiges
`format` ⇒ Fehler in JSON. Parse-Reihenfolge format → client → Rest.

## /v2/submit (+ /v2/submission_status)

Parameter: `format` (json/xml), `client`, `clientversion`, `user`
(= **Account-API-Key des Nutzers**, nicht der App-Key), je Index `#`:
`duration.#` (Pflicht, 1…32767), `fingerprint.#` (Pflicht),
`bitrate.#`, `fileformat.#`, `mbid.#` (**mehrfach erlaubt ⇒ je MBID
eine Submission-Zeile**), `track.#`, `artist.#`, `album.#`,
`albumartist.#`, `year.#`, `trackno.#`, `discno.#`, `puid.#`,
`foreignid.#` (`vendor:id`). `wait` wird geparst, aber ignoriert.
`fix_meta` normalisiert Whitespace, verwirft track_no/disc_no > 10000.

⚠️ **Stille Verwerfung:** Submission ohne MBID, PUID und ohne jedes
Textmetadatum wird kommentarlos verworfen (kein Eintrag in
`submissions[]`).

Antwort: `{"status":"ok","submissions":[{"id":…,"status":"pending"}]}` —
`status` immer `"pending"` (asynchrone Verarbeitung). `index` ist im
Code ein **String** (`"0"`) und fehlt ohne `.N`-Suffix (Doku zeigt
fälschlich Zahl).

`/v2/submission_status`: Parameter `format`, `client`, `clientversion`,
`id` (mehrfach, int; kein `user`). Antwort je ID `"pending"` oder
`"imported"` + `result.id` (Track-GID). Unbekannte IDs bleiben still
`"pending"`, nie 404.

## Application-Key & Nutzungsregeln

- Registrierung: https://acoustid.org/new-application (Login via
  MusicBrainz/Google/OpenID; Felder name, version, email, website).
  **Kein Freigabeprozess** — Key sofort aktiv (10 Zeichen).
- User-Key unter https://acoustid.org/api-key.
- Doku-Beispiel-Key ist ein rotierender Demo-Key.
- Nutzungsbedingungen (webservice.md): nicht-kommerziell frei
  (kommerziell → acoustid.biz), **„Do not make more than 3 requests per
  second"**, bei hohem Traffic vorab info@acoustid.org.
- Limits im Code: 4/s pro IP, 10/s pro Application (Default), 100/s
  global; 20-s-Sliding-Window (Burst = rate×20). Explizites
  App-Limit deaktiviert die IP-Prüfung.
- Upstream-Weiterleitung: kein Mechanismus für „im Namen Dritter" —
  **`user`-Key des Clients unverändert durchreichen** (Zweckbindung:
  „each user should provide their own"); eigener Application-Key als
  `client`. `/v2/user/create_anonymous` existiert, ist undokumentiert.

## Client-Verhalten

**Picard:** URL hart kodiert (`https://api.acoustid.org/v2`), Key
`v8pQ6oyB`; POST form-urlencoded ohne gzip; lookup-meta
`recordings releasegroups releases tracks compress sources`; kein
batch/maxdurationdiff; Submit gebatcht bis ~1 MB, bei 413 Batch ×0,7
(max 5 Retries); `mbid.N` nur bei Längendiff ≤30 s; ~3 req/s
Client-Limit; Retry bei 429/503/5xx (Retry-After ignoriert); nutzt
`submission_status` nicht; wertet `sources` fürs Ranking aus
(sources/max_sources > 0.25), kein Mindest-Score. Umbiegen nur per
Quelltext-Patch, Plugin-Monkeypatch (`AcoustIdAPIHelper.base_url` +
ratecontrol) oder DNS/TLS.

**beets/pyacoustid:** Basis-URL `http://api.acoustid.org/v2/`,
offiziell umbiegbar via `acoustid.set_base_url()`; Key `1vOwZtEn`;
POST **mit gzip-Body, aber nur bei http://-URLs**; lookup-meta
`recordings releases`; kein `clientversion`; Submit in 64er-Chunks;
0,33 s Abstand; **kein Retry** (Fehler ⇒ Chunk verworfen);
Mindest-Score 0.5 clientseitig; kein User-Agent.

## Konsequenzen für unsere Implementierung

Muss: GET+POST überall; Query+Body mergen; gzip-Bodys entpacken; 1-MiB
→ 413 (Picard-Batching); Chromaprint-Base64-Decoder; format
json/xml/jsonp (json reicht für Picard/beets); meta-Liste inkl.
Präzedenz, `sources` für Picard; `clientversion` optional; bekannte
Client-Keys je nach Whitelist-Schalter (DECISIONS); Fehlerformat exakt
mit HTTP-Mapping; `/v2/submission_status` mit Mehrfach-`id`; Submit
`status: "pending"`, `index` als String; mehrfach-`mbid.N`.
Soll: `batch`+`maxdurationdiff`; `puid`/`foreignid` tolerieren;
Score-Semantik (>0.4, max 10, dedup); CORS *; Limits 20/100.
Upstream: eigener App-Key; `user` durchreichen; ≤3 req/s;
Backoff-Queue (kein Retry-After); https.

## Offene Punkte

1. `index`-Typ im Submit-Response (Code: String; Doku: Zahl) — String
   liefern, kein Client wertet es aus.
2. Stille Verwerfung metadatenloser Submissions nachbilden oder
   abweichen? (Design-Entscheid Phase 11.)
3. `wait`-Parameter tolerieren, nicht implementieren.
4. Picards `recordingid`-Parameter existiert serverseitig nicht (nur
   `trackid`) — Fehler 2 wie im Original, kein Handlungsbedarf.
5. acoustid.biz derzeit HTTP 521; Preisangaben aus Wayback 2025-01.
6. Score-Serialisierung (Nachkommastellen) nur per Live-Request
   klärbar — für Kompatibilität irrelevant.
7. `usermeta`-Ausgabestruktur komplex; Priorität niedrig (kein
   relevanter Client nutzt es).
8. Upstream-Backoff-Modell: Vorschlag exponentiell ab 1 s, Deckel 30 s,
   persistente Queue.
