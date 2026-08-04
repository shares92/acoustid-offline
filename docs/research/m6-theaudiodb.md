# M6-Recherche: TheAudioDB — API, Key-Klassen, Limits, ToS, Betriebsrisiko

Stand: 2026-08-04. Gate-Recherche für **M6 — TheAudioDB-Proxy-Cache**
(`docs/HANDOFF.md` v2 §4, §6.4, §7 `tadb.api_key`, §9 „TheAudioDB-kompatibel",
§14.4, §15.2.6).

**Methode:** Websuche + Auswertung der Originaldokumente auf theaudiodb.com
**und empirische Einzel-Requests** gegen die Live-API mit dem öffentlichen
Test-Key `123` (sparsam; einmalig ein kontrollierter Burst für den
Rate-Limit-Test). Alle empirisch belegten Aussagen sind mit **[E]**
markiert und mit dem tatsächlich abgesetzten Request zitiert;
alles Übrige ist Dokumentations-/Websuchbeleg.

**Status: Entwurf zur Betreiber-Entscheidung.** Es wurde nichts umgebaut,
kein Code geändert, keine Steuerungsdatei angefasst.

---

## 0. Kernbefund in fünf Sätzen

1. Es gibt **drei Stufen**: Free (0 €, Key `123`, öffentlich geteilt),
   **Single Developer 8 €/Monat**, Small Business 20 €/Monat
   (+ Lifetime 295 $ / 999 $).
2. Die Preisseite behauptet, MBID-Lookups und Suche seien Premium-Features —
   **das ist falsch [E]**: mit Key `123` funktionieren `artist-mb.php`,
   `album-mb.php`, `track-mb.php`, `search.php` und sogar `artist-social.php`.
   Der wahre Unterschied ist die **Trefferzahl**: Free liefert **genau 1
   Datensatz pro Antwort**, Premium 10/50/100/500 je Methode.
3. Für den Projekt-Anwendungsfall (Anreicherung *pro MBID*, also genau ein
   Artist/Album/Track) reicht Free technisch — **aber** Tracklisten
   (`track.php?m=<albumid>`), Diskografien, Musikvideo-Listen und Suche sind
   auf 1 Treffer gekappt und damit unbrauchbar.
4. **Cachen ist ausdrücklich erlaubt:** „You can scrape, copy and modify any
   content returned from the API, as long as you use the official end points."
   Keine TTL-Vorgabe → der unbegrenzte Cache aus §4 ist ToS-konform.
   Pflichten: Quellenangabe (bezahlte Nutzung), kein Website-Scraping, kein
   Weiterverkauf der API, Verantwortung für Dritt-Zugriffe.
5. Größtes ungelöstes Design-Problem: die JSON-Antworten enthalten
   **absolute Bild-URLs auf `r2.theaudiodb.com`** — ein rein transparenter
   JSON-Proxy macht das Artwork **nicht** offline (§6, Frage F1).

---

## 1. Key-Klassen, Endpunkte, Felder

### 1.1 Die drei Stufen (Quelle: /pricing, /free_music_api, /api_apply.php)

| Stufe | Preis | Beworbene Merkmale | Rate-Limit (dokumentiert) |
|---|---|---|---|
| **Free Music API** | 0 €/Mo | „Lookup by Artist ID", „List Artist Albums", „Artist Discographies", „Album and Track ID Lookups" | 30 Requests/Min |
| **Single Developer** | **8 €/Mo** (Patreon/PayPal) | „Search by Artist, Album or Track metadata", „Lookup via MusicBrainz ID", „Access to all premium API methods", „Music Video Youtube Lookups" | 100/Min |
| **Small Business** | 20 €/Mo | „120 requests a min", „Artist social media links", „Dedicated email support", „Private API Key" | 120/Min |
| Lifetime | 295 $ (privat) / 999 $ (Business) | einmalig statt monatlich | (wie zugeordnete Stufe) |

Der Free-Key ist **öffentlich und geteilt**: „The current free API key is: 123."
`api_apply.php` sagt nur noch: „Due to massive demand we had to restrict our
free API tier. Apps should now join our $8 Patreon. Once signed up we will
email you a private key." Premium-Keys stehen im Nutzerprofil.

### 1.2 Was die Preisseite verschweigt — der reale Unterschied [E]

Die Doku-Seite `/free_music_api` führt pro Methode „**Free Limit**" und
„**Premium Limit**" — das sind **Trefferzahl-Kappungen**, keine Zugriffssperren:

| Methode | Free Limit | Premium Limit |
|---|---|---|
| Search Artist / Discography | 1 | 10 |
| Search Album (`a`-Parameter) | 1 | 100 |
| Search Track (`t`-Parameter) | 1 | 100 |
| Lookup Artist / Album / Track | 1 | 1 |
| Music Videos (`mvid`) | 1 | 50 |
| Music Charts (`track-top10`, `mostloved`) | 1 | 10 |
| Trending | 1 | 10 |
| v2 `list/discography` | — (v2 = Premium) | 500 |

**Empirisch bestätigt [E]** — jede Antwort mit Key `123` enthielt exakt einen
Datensatz, unabhängig davon wie viele es real gibt:

| Request (Key `123`) | HTTP | Bytes | Datensätze |
|---|---|---|---|
| `search.php?s=coldplay` | 200 | 30 098 | `artists`: **1** |
| `search.php?s=kettel` (Nischenkünstler) | 200 | 1 952 | `artists`: **1** |
| `artist-mb.php?i=cc197bad-…4234` | 200 | 30 098 | `artists`: **1** (voll) |
| `album.php?i=111239` (alle Coldplay-Alben) | 200 | 23 129 | `album`: **1** |
| `album-mb.php?i=120c786d-…35bf` (Release-Group) | 200 | 23 129 | `album`: **1** (voll) |
| `track.php?m=2109614` (**Tracklist eines Albums**) | 200 | 1 759 | `track`: **1** ← *„Politik" statt 11 Titel* |
| `track-mb.php?i=58b961e1-…3cd8` (Recording) | 200 | 1 759 | `track`: **1** (voll) |
| `discography.php?s=coldplay` | 200 | **62** | `album`: **1** |
| `discography-mb.php?s=cc197bad-…` | 200 | **62** | `album`: **1** |
| `mvid-mb.php?i=cc197bad-…` | 200 | 1 187 | `mvids`: **1** |
| `searchalbum.php?s=coldplay&a=Parachutes` | 200 | 12 623 | `album`: **1** |
| `artist-social.php?i=111239` | 200 | 123 | `artists`: **1** ← *laut Preisseite „Business"* |

**Wichtig:** Die *Feldtiefe* ist auf Free **nicht** beschnitten [E] — der
Artist-Datensatz enthält alle 47 Felder inkl. `strArtistFanart`…`Fanart4`,
`strArtistLogo`, `strArtistCutout`, `strArtistClearart`, `strArtistBanner`,
`strArtistWideThumb`, alle 14 Sprach-Biografien; der Album-Datensatz alle 63
Felder inkl. `strAlbumThumb`, `strAlbumCDart`, `strAlbumSpine`, `strAlbumBack`,
`strAlbum3DCase/3DFace/3DFlat/3DThumb` sowie Fremd-IDs (`strDiscogsID`,
`strMusicBrainzID`, `strMusicBrainzArtistID`, `strSpotifyID`, `strItunesID`,
`strWikidataID`, `strUPCID`); der Track-Datensatz alle 68 Felder inkl.
`strTrackLyrics`, `strISRC`, `strMusicVid` und Audio-Features
(`intTempo`, `intEnergy`, `intDanceability`, `strKey`, …).

### 1.3 MBID-Lookups — die für das Projekt entscheidende Familie [E]

Alle drei MBID-Einstiege funktionieren **auf der Free-Stufe** und liefern den
vollständigen Einzeldatensatz:

| Lokaler Einstieg | Upstream-Endpunkt | MBID-Typ |
|---|---|---|
| Artist | `artist-mb.php?i={mbid}` | **Artist-MBID** |
| Album | `album-mb.php?i={mbid}` | **Release-Group-MBID** (nicht Release!) |
| Track | `track-mb.php?i={mbid}` | **Recording-MBID** |
| Diskografie | `discography-mb.php?s={artist-mbid}` | Artist-MBID (Free: 1 Treffer) |
| Musikvideos | `mvid-mb.php?i={artist-mbid}` | Artist-MBID (Free: 1 Treffer) |
| Charts | `track-top10-mb.php?s={artist-mbid}` | Artist-MBID (Free: 1 Treffer) |

⚠️ **Achtung für §9/§6.5:** `album-mb.php` erwartet die
**Release-Group-MBID**, das Projekt arbeitet aber durchgängig mit
**Release-MBIDs** (`/v1/release/{mbid}`, `/caa/release/{mbid}/front`,
`covers.artwork.release_mbid` PK). Die Auflösung Release → Release-Group muss
aus dem MB-Spiegel kommen (`release.release_group`) — sonst laufen alle
TADB-Album-Abrufe ins Leere. Das ist ein **neuer, im HANDOFF nicht erfasster
Abhängigkeitspunkt** von M6 auf die MB-Query-Schicht (§8 „Externe Referenzen").

### 1.4 Artwork-Typen und -Größen (Quelle: /docs_artwork, HEAD-Requests [E])

Bilder liegen auf **`r2.theaudiodb.com`** (Cloudflare R2), **ohne API-Key
abrufbar [E]**. Kleinere Varianten per Suffix `/medium`, `/small`
(zusätzlich existiert `/preview` — **empirisch byte-identisch mit `/small`**,
gleicher ETag).

| Artist | Größen | Format | | Album | Größen | Format |
|---|---|---|---|---|---|---|
| Thumb | 700/350/250/100 | JPG | | Thumb | **700**/500/250/100 | JPG |
| Logo | 800×310 … | PNG | | 3D Thumb | 730 … | PNG |
| Cutout | 700 … | PNG | | Back | 700/500/200 | JPG |
| Clear Art | 1000×562 … | PNG | | 3D Case | 1000×720 … | PNG |
| Banner | 1000×185 … | JPG | | CD Art | 1000×1000 … | PNG |
| Wide Thumb | 1000×562 … | JPG | | Flat | 1000×460 … | PNG |
| Fanart (×4) | 1280×720 | JPG | | Spine | 35×700 | JPG |

**Folgen für die Cover-Kette (§4/§7):** `strAlbumThumb` ist **maximal
700×700 px [E]** (gemessen: `…/album/thumb/vsusvs1521243711.jpg` = 700×700,
56 911 Byte) — also **unter** dem Projektziel „max. 1200 px lange Kante".
`strAlbumThumbHQ` war im Test `null` [E]. TheAudioDB liefert in der Kette
CAA → **TADB** → Discogs also systematisch das kleinere Bild; nicht
hochskalieren, sondern in `covers.artwork` die realen Abmessungen
protokollieren. Als Frontcover ist ausschließlich `strAlbumThumb` geeignet
(CDart/Spine/Back/3D sind keine Frontcover).

Bild-Header [E]: `ETag`, `Last-Modified` vorhanden, **kein `Cache-Control`**,
`cf-cache-status: DYNAMIC`. Konditionale Revalidierung (`If-None-Match`) ist
damit für Bilder möglich — für JSON nicht (s. §4.4).

---

## 2. Rate-Limits

### 2.1 Dokumentiert (Quelle: /free_music_api, Abschnitt „Rate Limit")

> „You will recieve a '429' http header if you breach the limit, then you will
> need to wait another minute until requests will work again."
> **Free users 30 requests per minute. Premium 100 per minute.
> Business 120 per minute.**

Fenster = 1 Minute, Reaktion = HTTP 429, Erholung = eine Minute warten.

### 2.2 Empirisch [E] — die Limits wurden nicht durchgesetzt

Kontrollierter Burst: **45 sequentielle Requests** auf
`discography.php?s=coldplay` (62 Byte Antwort) mit Key `123`, so schnell wie
möglich → **45× HTTP 200 in 8 Sekunden, kein einziges 429**. Das entspricht
~5,6 Req/s bzw. dem 11-fachen des dokumentierten Free-Limits.

Antwort-Header eines normalen v1-Requests [E]:
```
HTTP/2 200 · content-type: application/json · server: cloudflare
x-powered-by: PHP/8.4.14 · access-control-allow-origin: *
cf-cache-status: DYNAMIC
```
**Keine** `X-RateLimit-*`-Header, **kein** `Retry-After`, **kein**
`Cache-Control`, **kein** `ETag` auf JSON.

**Bewertung:** Das Limit ist derzeit nicht (oder nur lastabhängig) scharf
geschaltet. Darauf darf man sich **nicht** verlassen — Betreiber ist ein
kleines Team, das die Drossel jederzeit nachziehen kann. Der Proxy muss
selbst drosseln und 429 sauber behandeln (kein `Retry-After` → feste 60 s
Backoff, danach ein Versuch, dann Aufgeben + Event-Log).

**Empfehlung Konfiguration:** neuer Schlüssel `tadb.rate_per_s`,
**Default 0,5/s** (= 30/Min, free-konform), bei Premium-Key auf 1,5/s
hochsetzbar. Passt in §7 („Rate-Limits upstream … TheAudioDB gemäß
Key-Klasse. Exakte Werte: Recherche §14") — hiermit beziffert.

### 2.3 Latenz [E]

5 Messungen `artist-mb.php` aus Europa: TTFB 0,67 s (kalt, inkl.
TLS-Handshake), danach **0,11–0,21 s**, Gesamt 0,12–0,70 s.
Drittanbieter-Monitore nennen als Durchschnitt ~1,65 s — deutlich schlechter
als hier gemessen; für einen Lazy-Cache-Miss ist beides unkritisch.

---

## 3. Cache- und ToS-Frage (Risiko §15.2.6)

Quelle: <https://www.theaudiodb.com/docs_terms_of_use.php>, „Last updated:
01/07/2025", Betreiber **TheDataDB Ltd**.

### 3.1 Cachen ist ausdrücklich erlaubt

> **Content use:** „You can scrape, copy and modify any content returned from
> the API, as long as you use the official end points. Please do not scrape
> our website. You also cannot remove or alter any copyright or trademark
> notices."

Das ist die für §4/§6.4 entscheidende Klausel: **Kopieren und Verändern von
API-Antworten ist gestattet**, ohne Zeitgrenze. Es gibt **keine** ToS-Vorgabe
zu Cache-Dauer, Löschfristen oder Re-Validierung. Der geplante **unbegrenzte
Cache mit ausschließlich manueller Invalidierung ist damit ToS-konform.**
Die Bedingung „as long as you use the official end points" ist erfüllt — der
Proxy ruft die offizielle API auf und scrapt die Website nicht.

### 3.2 Free vs. Paid — die relevante Nutzungsgrenze

> **Free API Usage:** „You may use our API to lookup data and artwork for your
> development projects. You cannot publish apps to an appstore unless you are
> a paid subscriber."
>
> **Paid API Usage:** „You may use our API to develop apps and services as
> long as you stay within the rate limit. You can use our custom artwork in
> your projects but must mention us as the source of the data."

Ein selbst gehostetes, privates Gateway im LAN/VPN ist kein App-Store-Release
→ Free wäre formal zulässig. **Aber:** primärer Client ist DroppedNeedle, die
eigene App des Auftraggebers. Sobald diese in einen App Store geht, ist die
Free-Stufe unzulässig — unabhängig davon, dass der Aufruf technisch über das
eigene Gateway läuft. Das ist ein harter Grund für die Bezahlstufe.

### 3.3 Attribution

Pflicht auf der bezahlten Stufe: „**must mention us as the source of the
data**". Zusätzlich beim Artwork: „Most of our artwork is custom and is
created by our users, you must not pass it off as your own and **should link
back to our website where appropriate**." Trademark-Logos „**As is**", nicht
verändern. Der `strCreativeCommons`-Hinweis der ToS greift ins Leere —
**das Feld war in keiner Antwort enthalten [E]** (weder Artist noch Album);
CC-Status ist also über die API nicht prüfbar.

**Umsetzung im Projekt:** README-Abschnitt + Fußzeile der Admin-UI
„Metadaten und Artwork von TheAudioDB.com" mit Link auf
<https://www.theaudiodb.com>; im `/v1/identify`-Antwortblock `tadb` ein
`attribution`-Feld mitführen, damit auch DroppedNeedle die Angabe rendern
kann. Passt zu §6.4 und zur bestehenden Lizenz-Zeile in §2.

### 3.4 Ausdrücklich verboten / riskant

| ToS-Klausel | Wortlaut (gekürzt) | Bezug zum Projekt |
|---|---|---|
| Website-Scraping | „Please do not scrape our website." | unkritisch — nur API |
| Copyright-Vermerke | „cannot remove or alter any copyright or trademark notices" | Bilder unverändert cachen, nicht rebranden |
| **Reselling** | „You cannot resell our API in any way without specific permission." | **kritisch bei öffentlicher Exponierung des Proxys** |
| **Dritt-Zugriff** | „You are responsible for ensuring that any third parties you give access to the API comply with the terms of use." | **kritisch** — dito |
| Sicherheit | „cannot compromise the security of the API" | unkritisch |
| Fremdinhalte | Dritt-Content nur mit Erlaubnis | unkritisch |
| Haftung | Anbieter begrenzt Haftung | „as is", kein SLA |

**Bewertung Risiko §15.2.6 — von „niedrig" auf „niedrig–mittel" anzuheben,
mit klarer Auflage:** Ein `/tadb/…`-Proxy, der mit dem Key des Betreibers
Anfragen **Dritter** bedient, ist funktional eine Weitergabe des API-Zugangs.
Solange das Gateway im LAN/VPN läuft bzw. bei Exponierung zwingend
`auth.mode=apikey` gesetzt ist (§2 fordert das ohnehin), bewegt man sich
innerhalb der ToS — dann sind alle Nutzer der Betreiber selbst bzw. von ihm
autorisiert und er haftet für deren Verhalten, was die ToS genau so vorsehen.
**Auflage für M6:** `/tadb/…` darf im Modus `auth.mode=none` **nicht** an ein
öffentliches Interface gebunden werden; README + Admin-UI müssen davor warnen.
Refunds: 14 Tage; Kündigung direkt über Patreon/PayPal.

---

## 4. API-Form — Pfadschema, Antwortstruktur, Eigenheiten

### 4.1 Basis-URLs und Authentifizierung

```
v1: https://www.theaudiodb.com/api/v1/json/{APIKEY}/{methode}.php?{params}
v2: https://www.theaudiodb.com/api/v2/json/{gruppe}/{typ}/{wert}
    + Header:  X-API-KEY: {APIKEY}
```

**v1 trägt den Key als Pfadsegment**, v2 im Header. `http://` → 301 auf
`https://`, `theaudiodb.com` → 301 auf `www.theaudiodb.com` [E]; der Proxy
sollte direkt `https://www.theaudiodb.com` ansprechen und Redirects nicht
folgen müssen.

### 4.2 Vollständige v1-Methodenliste (Quelle: /free_music_api)

**Search:** `search.php?s=`, `discography.php?s=`, `discography-mb.php?s=`,
`searchalbum.php?s=&a=`, `searchtrack.php?s=&t=`
**Lookup:** `artist.php?i=`, `artist-mb.php?i=`, `artist-social.php?i=`,
`album.php?i=` (Artist-ID), `album.php?m=` (Album-ID), `album-mb.php?i=`,
`track.php?m=` (Album-ID), `track.php?h=` (Track-ID), `track-mb.php?i=`
**List:** `mvid.php?i=`, `mvid-mb.php?i=`, `track-top10.php?s=`,
`track-top10-mb.php?s=`, `mostloved.php?format=track|album`,
`trending.php?country=&type=itunes&format=albums|singles`

Die in der Doku noch verlinkte `artist-links.php` ist **tot: HTTP 404 [E]** —
Nachfolger ist `artist-social.php` [E]. Die Doku ist also stellenweise veraltet;
ein Proxy darf **keine Whitelist fester Methodennamen** hart verdrahten,
sondern muss beliebige `*.php`-Pfade durchreichen (siehe F2).

### 4.3 v2 (nur Premium) [E]

```
/api/v2/json/search/{artist|album|track}/{text}
/api/v2/json/lookup/{artist|album|track}/{id}
/api/v2/json/lookup/{artist_mb|album_mb|track_mb}/{mbid}
/api/v2/json/list/discography/{idArtist}
```
Empirisch [E]:
- ohne Header → `HTTP 400 {"Message":"Missing API key in header, sign up at https://www.theaudiodb.com/pricing"}`
- mit `X-API-KEY: 123` → `HTTP 400 {"Message":"Invalid Premium API key: Signup here: …"}`

→ **v2 ist mit dem Free-Key definitiv nicht nutzbar.** Die Doku sagt zudem:
„V2 is only for premium subscribers and **will be the only version developed
going forward**."

### 4.4 Antwortstruktur und Konventionen [E]

- Hülle: ein einziger Schlüssel je Methode — `{"artists":[…]}`,
  `{"album":[…]}` (Singular!), `{"track":[…]}`, `{"mvids":[…]}`.
- **Kein Treffer → HTTP 200 mit `{"artists":null}`** [E]
  (getestet mit `artist-mb.php?i=00000000-0000-0000-0000-000000000000`).
  Also **kein 404** — der Cache muss `null` als gültiges Negativ-Ergebnis
  behandeln, sonst schlagen dieselben MBIDs ewig upstream durch.
- Alle Werte sind **Strings oder `null`**, auch Zahlen: `"intYearReleased":"2002"`,
  `"idArtist":"111239"` [E]. Keine echten JSON-Zahlen, kein Boolean.
- Feld-Chaos aus der Historie: `strMusicBrainzID` bedeutet je nach Entität
  Artist-MBID / **Release-Group**-MBID / Recording-MBID; daneben existieren
  `strMusicBrainzArtistID` und `strMusicBrainzAlbumID` [E].
- **Ungültiger Key → HTTP 404 mit HTML-Fehlerseite** (IIS-Style, 1 245 Byte,
  `Content-Type: text/html`) [E] — **nicht** JSON. Der Proxy muss auf
  `Content-Type` prüfen und **nur `application/json` mit parsebarem Inhalt
  cachen**; HTML-404, 429 und 5xx niemals persistieren.
- CORS ist offen (`access-control-allow-origin: *`) [E].
- Bild-URLs in der Antwort sind **absolut** auf `https://r2.theaudiodb.com/…` [E].

### 4.5 Versionierung / Stabilität

v1 ist „written over 10 years ago", wird laut Doku nicht weiterentwickelt,
aber auch nicht als deprecated markiert; v2 ist die Zukunft und
premium-exklusiv. Ein Versionswechsel wäre für M6 ein **Bruch**: anderes
Pfadschema, anderer Auth-Ort (Header statt Pfad), andere Antwortstruktur.
OpenAPI/Postman/MCP sind auf der Doku-Seite angekündigt, aber
**nicht ausgeliefert** — vier plausible Spec-URLs getestet, alle 404 [E].
Es gibt also **keine maschinenlesbare Vertragsdatei**, gegen die man testen
könnte; ein eigener Vertragstest gegen Live-Antworten (Golden-Files aus dem
Cache) ist der einzige Weg.

---

## 5. Betriebsrisiko

**Anbieter:** TheDataDB Ltd, kleines Team („zag" u. a.), Schwesterprojekt
TheSportsDB, Site seit 2012, 14 765 registrierte Mitglieder. Finanzierung
über Patreon/PayPal — Ein-Personen-/Kleinteam-Risiko.

**Aktivität 2026 [E-nah]:** Forum aktiv — jüngste Beiträge vom
**4. August 2026** („Apply to be an editor"), 4. Mai 2026, 8. Juli 2026.
Copyright-Zeile „© 2012-2026". API antwortet schnell und vollständig.
Der Dienst ist zum Recherchezeitpunkt **lebendig und gepflegt**.

**Bekannte Störungen:** Kodi-Forum meldet für **August 2024** einen fast
ganztägigen Ausfall (Site und API, 404). Ältere Threads dokumentieren
wiederholt „API-Key funktioniert nicht" — die Free-Keys wurden über die Jahre
mehrfach gewechselt (`1` → `2` → `195003` → `123`) und alte Keys still
abgeschaltet. **Das ist das realistischste Betriebsrisiko: nicht der Tod des
Dienstes, sondern ein stiller Key-/Limit-Wechsel.** Ein privater Bezahl-Key
ist davon deutlich weniger betroffen als der geteilte Free-Key. Keine
offizielle Statusseite gefunden; Kanäle sind Forum und Discord.

**Was heißt das für den unbegrenzten Cache?**
- Der Cache ist der **einzige** Bestand — anders als AcoustID/Discogs/CAA gibt
  es keinen Dump und keinen Bulk-Bezugsweg. Stirbt TheAudioDB, ist der
  lokale Cache das Archiv und wächst nie wieder.
- **Konsequenz 1 (Backup):** `/data/tadb` gehört ins Backup. §7 kennt nur
  `backup.include_covers` (Default `false`); der TADB-Cache ist laut §15.1
  „einstellige GB" — Empfehlung: **immer mitsichern**, nicht optional.
- **Konsequenz 2 (Sichtbarkeit):** `/status` sollte den TADB-Cache-Umfang
  (Einträge, Bytes, ältester/jüngster `fetched_at`) und den letzten
  Upstream-Status zeigen, damit ein toter Dienst auffällt statt still zu
  degradieren.
- **Konsequenz 3 (Degradation):** Bei Upstream-Ausfall greift bereits §9:
  `null`-Block mit Grund `not_cached_offline` statt Gesamtfehler. Zusätzlich
  nötig: **Circuit-Breaker**, damit nicht jede Anfrage in einen Timeout läuft
  und dabei den Idle-Auto-Stopp blockiert.
- **Konsequenz 4 (Migrationsfähigkeit):** Cache-Einträge mit `endpoint`,
  `params`, `fetched_at`, `http_status` und Roh-JSON ablegen — dann ist ein
  Wechsel auf v2 oder eine Fremdquelle ohne Datenverlust möglich.

---

## 6. Offene Fragen — Optionen und Empfehlung

### F1 — Bild-URLs: transparent lassen oder umschreiben? (wichtigster Punkt)

Die gecachte JSON enthält absolute `r2.theaudiodb.com`-URLs. Wer sie 1:1
ausliefert, hat zwar die JSON offline, das **Artwork aber weiterhin online** —
das verfehlt das Projektziel „ohne laufende Abhängigkeit von den öffentlichen
Diensten" (§1) für genau den Teil, der in §4 als „Cache-Proxy" versprochen ist.

- **A) 1:1 durchreichen.** Byte-transparent, minimaler Aufwand; Artwork
  bleibt online-abhängig, Offline-Versprechen halb eingelöst.
- **B) Beim Cachen umschreiben** auf `/tadb/images/…` + Bilder lazy spiegeln.
  Vollständig offline; die Antwort weicht vom Original ab (Kompatibilitäts-
  bruch für Clients, die exakt die Original-URL erwarten).
- **C) ⭐ Beides trennen (Empfohlen):** `/tadb/…` bleibt **byte-transparent**
  (Kompatibilität für Fremdclients), zusätzlich ein Spiegel-Endpoint
  `/tadb/images/{pfad}` → `r2.theaudiodb.com/{pfad}` mit demselben unbegrenzten
  Lazy-Cache; die **vereinheitlichte** API (`/v1/identify`, `/v1/release`)
  liefert im `tadb`-Block **umgeschriebene lokale URLs**. Ein
  Config-Schalter `tadb.rewrite_image_urls` (Default `false`) erlaubt es,
  auch `/tadb/…` umschreiben zu lassen.
  *Begründung:* Der eigene Client DroppedNeedle nutzt ohnehin `/v1/…` und wird
  damit echt offline; die Kompatibilitätsfassade bleibt unangetastet. Bilder
  brauchen **keinen** API-Key [E], der Spiegel ist also trivial.

### F2 — Pfadschema des Proxys: Wie wird der Key-Pfadteil behandelt?

v1 trägt den Key als Pfadsegment — der lokale Pfad darf ihn nicht enthalten
(sonst leakt der Betreiber-Key an jeden Client).

- **A) `/tadb/{methode}.php?…`** — kurz, aber Clients, die die Original-URL
  bauen (inkl. Key-Segment), funktionieren nicht ohne Umbau.
- **B) ⭐ `/tadb/api/v1/json/{beliebig}/{methode}.php?…` (Empfohlen):**
  Das Key-Segment wird **entgegengenommen und verworfen**, serverseitig durch
  `tadb.api_key` ersetzt. Damit funktioniert ein Client, der schlicht
  `www.theaudiodb.com` → `gateway:8080/tadb` ersetzt, **unverändert** — genau
  das „Umbiegen der Basis-URL" aus Erfolgskriterium 1. Zusätzlich `A` als
  Kurzform akzeptieren. Für v2 analog `/tadb/api/v2/json/…` mit
  serverseitig gesetztem `X-API-KEY`, sobald ein Premium-Key vorliegt.
- **C) Nur v1 unterstützen.** Weniger Code, aber der Weg nach v2 ist versperrt.

### F3 — Cache-Schlüssel und Negativ-Einträge

`{"artists":null}` bei HTTP 200 ist der Normalfall für „kein Treffer" [E].

- **A) Nicht cachen.** Jede unbekannte MBID schlägt dauerhaft upstream durch —
  bei einem Offline-Gateway inakzeptabel.
- **B) Wie Treffer unbegrenzt cachen.** Konsequent zu §4, aber später
  ergänzte Künstler/Alben werden nie gesehen.
- **C) ⭐ Negativ-Einträge mit Wiederholintervall (Empfohlen):**
  eigener Schlüssel `tadb.negative_retry_days`, **Default 30** — analog zum
  bereits festgelegten `covers.negative_retry_days`. Positivtreffer bleiben
  unbegrenzt (§4 unverändert). Cache-Schlüssel = normalisierter
  Methodenname + alphabetisch sortierte Query-Parameter **ohne** Key-Segment.

### F4 — Welche Key-Stufe soll der Betreiber besorgen? (Gate-Frage)

- **A) Free-Key `123` (0 €).** Reicht für reine Einzel-MBID-Anreicherung [E],
  aber: 1 Treffer pro Antwort → **keine Tracklisten, keine Diskografien,
  keine Suche**; geteilter Key ohne Kontingentschutz; historisch mehrfach
  still abgeschaltet; ToS verbietet App-Store-Veröffentlichung des Clients.
- **B) ⭐ Single Developer, 8 €/Monat (Empfohlen).** Privater Key, 100 Req/Min,
  volle Trefferzahlen (Tracklist 100, Diskografie 10, Videos 50), Zugang zur
  v2-API, ToS-konform auch wenn DroppedNeedle veröffentlicht wird, Pflicht
  zur Quellenangabe — die man ohnehin geben sollte. *Begründung:* Für 8 €
  fallen sämtliche funktionalen Kappungen weg, der Key ist nicht mehr
  öffentlich geteilt, und die einzige Alternative mit vollem Funktionsumfang
  (Business) kostet das 2,5-fache für Merkmale, die ein Ein-Nutzer-Gateway
  mit unbegrenztem Cache nicht braucht.
- **C) Small Business, 20 €/Monat.** 120 statt 100 Req/Min und E-Mail-Support.
  Bei Lazy-Cache-Betrieb (Default 0,5 Req/s) ist der Limit-Unterschied
  bedeutungslos; `artist-social.php` funktioniert **bereits auf Free** [E].
  Nur sinnvoll, wenn Support-SLA gewünscht ist.
- **D) Lifetime 295 $.** Amortisiert nach ~37 Monaten gegenüber B — nur
  attraktiv, wenn man dem Anbieter über 3+ Jahre Bestand zutraut; angesichts
  Kleinteam-Risiko (§5) eher nicht.

**Zwischenlösung:** M6 kann **sofort mit dem Free-Key `123` implementiert und
getestet** werden — das Pfadschema, der Cache und die Fehlerbehandlung sind
identisch. Der Betreiber-Key wird über `tadb.api_key` nachgereicht; leer =
Quelle aus (§7). M6 ist damit **kein blockierendes Gate** — nur die volle
Datentiefe hängt am 8-€-Key.

### F5 — Release-MBID → Release-Group-MBID

`album-mb.php` braucht die Release-**Group**-MBID, das Projekt führt
Release-MBIDs (§1.3).

- **A) Über den MB-Spiegel auflösen** (`release.release_group`) — eine
  zusätzliche Query in der gekapselten Query-Schicht (§8).
- **B) `searchalbum.php?s=<artist>&a=<album>` als Fallback** — unscharf,
  auf Free auf 1 Treffer gekappt, fehleranfällig.
- **C) ⭐ A mit Fallback auf B (Empfohlen):** primär exakt über den Spiegel;
  ist der MB-Spiegel offline (degradierter Betrieb laut §6), greift die
  Namenssuche. Die zusätzliche MB-Query gehört in dieselbe versionierbare
  Query-Schicht wie die CAA-Verzeichnis- und Discogs-Relationship-Abfragen.

### F6 — Muss der TADB-Cache ins Backup?

- **A) Wie bisher optional** (`backup.include_covers`-Logik übertragen).
- **B) ⭐ Immer mitsichern (Empfohlen):** Der Bestand ist nicht
  wiederbeschaffbar (kein Dump, kein Bulk-Weg) und laut §15.1 nur einstellige
  GB groß. Ein Verlust bedeutet vollständigen Neuaufbau über Monate von
  Lazy-Abrufen — oder gar keinen, falls der Dienst bis dahin verschwunden ist.

---

## 7. Konkrete Änderungsvorschläge am HANDOFF (nach Betreiber-Entscheid)

| § | Änderung |
|---|---|
| §7 Config | neu: `tadb.rate_per_s` (0.5), `tadb.negative_retry_days` (30), `tadb.rewrite_image_urls` (false) |
| §7 Feste Werte | „TheAudioDB gemäß Key-Klasse" beziffern: Free 30/Min, Premium 100/Min, Business 120/Min; Proxy-Default 0,5 Req/s |
| §8 Dateisystem | `/data/tadb/json/…` (Roh-JSON) und `/data/tadb/images/…` (Spiegel von r2) trennen |
| §8 Externe Referenzen | MB-Query „Release-MBID → Release-Group-MBID" ergänzen (F5) |
| §9 TheAudioDB | Pfadschema aus F2 festschreiben; Bild-Spiegel `/tadb/images/…`; nur `application/json` wird gecacht |
| §9 Betrieb | `/status` um TADB-Cache-Kennzahlen + letzten Upstream-Status erweitern |
| §7 Backup | TADB-Cache verbindlich einschließen (F6) |
| §15.2.6 | Risiko auf „niedrig–mittel" mit Auflage: `/tadb/…` nie öffentlich ohne `auth.mode=apikey`; Attribution in README + UI |
| §4 Cover-Politik | Hinweis: TADB liefert max. 700×700 — unter dem 1200-px-Ziel, nicht hochskalieren |

---

## 8. Quellen

**Primär (TheAudioDB):**
- API-Doku: <https://www.theaudiodb.com/free_music_api>
- Preise: <https://www.theaudiodb.com/pricing>
- Key-Antrag: <https://www.theaudiodb.com/api_apply.php>
- Nutzungsbedingungen (Stand 01.07.2025): <https://www.theaudiodb.com/docs_terms_of_use.php>
- Artwork-Typen und -Größen: <https://www.theaudiodb.com/docs_artwork>
- JSON-Beispiele: <https://www.theaudiodb.com/docs_json>
- Forum (Aktivitätsnachweis): <https://www.theaudiodb.com/forum>
- Roadmap (Trello): <https://trello.com/b/V52egHeq/theaudiodb>
- Discord: <https://discord.gg/pFvgaXV>

**Sekundär:**
- Kodi-Forum, Ausfall August 2024 und Key-Historie: <https://forum.kodi.tv/showthread.php?tid=134260&page=11>
- „api key doesn't work": <https://www.theaudiodb.com/forum_topic.php?t=267>
- „Paid API Key": <https://www.theaudiodb.com/forum_topic.php?t=336&s=0>
- MrMC-Forum, veralteter Key: <https://forum.mrmc.tv/viewtopic.php?t=5755>
- Emby-Community, Patreon-Key-Eintrag: <https://emby.media/community/topic/146517-where-do-i-enter-my-theaudiodb-patreon-api-key/>
- API-Verzeichnisse (Latenzangabe ~1,65 s): <https://publicapis.io/audiodb-music-api>, <https://publicapi.dev/the-audio-db-api>, <https://apislist.com/api/910/theaudiodb>

**Empirische Requests [E]** (alle am 2026-08-04, Key `123`, User-Agent
`musicmeta-offline-research/1.0`): `search.php` (2×), `artist-mb.php` (3×),
`artist.php`, `artist-social.php`, `artist-links.php`, `album.php` (3×),
`album-mb.php`, `track.php` (2×), `track-mb.php` (2×), `discography.php`,
`discography-mb.php`, `mvid-mb.php`, `searchalbum.php`, Negativ-Test mit
Null-MBID, Bad-Key-Test, v2 ohne/mit Key, 5 Latenzmessungen,
Burst-Test 45 Requests, 6 Bild-HEADs auf `r2.theaudiodb.com`,
1 Bild-Download zur Größenmessung. Kein Schreibzugriff, kein Scraping der
Website über die drei Doku-Seiten hinaus.
