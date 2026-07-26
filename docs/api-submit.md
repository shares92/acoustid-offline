# API-Dienst: `/v2/submit` (Phase 11)

Referenz zum Submit-Endpunkt des Containers `acoustid-api`. Vertrag und
Begründungen: ARCHITECTURE §5.2, §5.3, §6 und §7 sowie
[docs/research/phase1-api-formate.md](research/phase1-api-formate.md) und
[docs/research/phase1-acoustid-index.md](research/phase1-acoustid-index.md).
Der Lookup steht in [docs/api-lookup.md](api-lookup.md).

**Stand:** Modi `off` und `local` vollständig; `local+upstream` verhält sich
in dieser Phase wie `local` — die Weiterleitung an api.acoustid.org und die
Statuspfade `forwarded`/`forward_failed` baut Phase 12.
`/v2/submission_status` folgt in Phase 13.

## Modi (`submit.mode`, ARCHITECTURE §6)

| Modus | Verhalten |
|---|---|
| `off` | Der Endpunkt nimmt nichts an: **Fehler 12 „not allowed" / HTTP 400**, geprüft noch vor dem Lesen der Parameter. |
| `local` (Default) | Speichern in `local_submission`, indexieren, Antwort `pending`. |
| `local+upstream` | In dieser Phase identisch mit `local`. Die Statusmaschine kann die Weiterleitung bereits abbilden. |

Warum **12** und nicht 13 („service currently unavailable"): der Zustand ist
kein Ausfall, sondern eine Entscheidung des Betreibers. Ein 503 würde Picard
und beets zum Wiederholen bewegen — beide werten 5xx/503 als „gleich nochmal"
—, und der Wächter benutzt 503 bereits für „Stack wacht gerade auf". Ein 400
mit Code 12 beendet den Versuch sofort und eindeutig.

## Parameter

Global (einmal je Anfrage):

| Name | Pflicht | Bedeutung |
|---|---|---|
| `client` | ja | Application-Key. Wird nur auf Anwesenheit geprüft (Prüfung macht der Wächter, ARCHITECTURE §7). |
| `user` | ja | Im Original der **Account-Key des Nutzers**. Diese Instanz hat keinen Benutzerbestand: der Wert wird verlangt, nie geprüft und für die spätere Weiterleitung mitgeschrieben. Fehler 6 kann hier nie entstehen. |
| `format` | nein | `json` (Default), `jsonp`, `xml`. |
| `jsoncallback` | nein | Funktionsname für `jsonp`, Default `jsonAcoustidApi`. |
| `clientversion` | nein | Wird gespeichert und geloggt. |
| `wait` | nein | Wird gelesen und **ignoriert** (Original-Verhalten). |

Je eingereichter Aufnahme, optional mit Suffix `.N`:

| Name | Pflicht | Bedeutung |
|---|---|---|
| `fingerprint.N` | ja | Komprimierter Chromaprint, URL-sicheres Base64 ohne Padding, nur Version 1. Sonst Fehler 3. |
| `duration.N` | ja | Länge in Sekunden, **1…32767**. Fehlt oder unlesbar ⇒ Fehler 2; außerhalb ⇒ Fehler 8. |
| `bitrate.N` | nein | Positive Ganzzahl, sonst Fehler 9. Unlesbar gilt als nicht angegeben. |
| `fileformat.N` | nein | Freitext (`FLAC`, `MP3`, …). |
| `mbid.N` | nein | Recording-MBID. **Mehrfach erlaubt** ⇒ je MBID eine Submission-Zeile mit eigener ID. Keine UUID ⇒ Fehler 7. |
| `puid.N` | nein | Legacy-PUID (UUID), sonst Fehler 7. |
| `foreignid.N` | nein | Form `vendor:id`, sonst Fehler 10. |
| `track.N`, `artist.N`, `album.N`, `albumartist.N` | nein | Textmetadaten, siehe `fix_meta`. |
| `year.N`, `trackno.N`, `discno.N` | nein | Ganzzahlen; `trackno`/`discno` siehe `fix_meta`. |

**Transport:** `GET` und `POST` gleichwertig; Parameter aus Query-String
**und** `application/x-www-form-urlencoded`-Rumpf (Query-String gewinnt bei
Namensgleichheit — außer bei `mbid.N`, wo **alle** Werte zählen);
`Content-Encoding: gzip` wird entpackt; Rumpfgrenze 1 MiB vor und nach dem
Entpacken (darüber Fehler 19 / HTTP 413 — Picard verkleinert daraufhin seine
Pakete); jede Antwort trägt `Access-Control-Allow-Origin: *`.

**Anzahl der Teilanfragen:** keine eigene Obergrenze. Es gilt allein die
1-MiB-Rumpfgrenze — genau die Größe, auf die Picard sein Batching stützt.

`fix_meta` (wie im Original, zwei Regeln):

- Textmetadaten werden im Whitespace normalisiert (`"  Der   Titel\n"` →
  `"Der Titel"`); was danach leer ist, gilt als nicht angegeben.
- `trackno`/`discno` **über 10000** werden verworfen — solche Werte stammen
  aus kaputten Tags. `year` bleibt unangetastet.

**Stille Verwerfung.** Eine Teilanfrage ohne MBID, PUID, `foreignid` und ohne
jedes Textmetadatum wird kommentarlos verworfen: kein Fehler, kein Eintrag in
`submissions[]`, keine Zeile in der Datenbank. Ein Fingerprint ohne jede
Zuordnung wäre auch tatsächlich wertlos — auffindbar, aber ohne Antwortinhalt.

**Fehler kippen die ganze Anfrage.** Anders als beim Lookup wird beim Submit
keine kaputte Teilanfrage still übersprungen; das Original prüft dort alle
Parameter im Voraus.

## Antwort

```json
{"status": "ok", "submissions": [{"id": 1234, "index": "0", "status": "pending"}]}
```

- `status` je Eintrag ist **immer** `"pending"` — auch wenn schon indexiert
  wurde. Das Original verarbeitet asynchron und kennt keinen anderen Wert.
- `index` ist eine **Zeichenkette** und fehlt ganz, wenn der Client ohne
  `.N`-Suffix eingereicht hat. (Die Original-Doku zeigt hier fälschlich eine
  Zahl; maßgeblich ist der Code.)
- `id` ist die Submission-ID aus `local_submission.id` — je MBID eine eigene.
- Wurde alles still verworfen, kommt `{"status": "ok", "submissions": []}`.

Fehler im üblichen Format; 19 Codes mit festem HTTP-Status
(`api/app/errors.py`), abweichend von 400 sind 5→500, 13→503, 14→429, 18→404,
19→413.

## Speicherung: `local_submission`

Eigene Einreichungen dürfen **nicht** in `track`/`fingerprint`/`track_mbid`
stehen. Der Delta-Importer schreibt dort ganze Zeilen per expliziter ID
(`INSERT … ON CONFLICT (id) DO UPDATE`, ARCHITECTURE §5.2 Import-Regel 2, und
er füllt fehlende Schlüssel bewusst mit NULL statt sie auszulassen — siehe
LEARNINGS zu `json_strip_nulls`). Eine lokal eingefügte Zeile würde beim
nächsten Tagesdelta still überschrieben, sobald upstream dieselbe ID vergibt —
und upstream vergibt sie garantiert, weil dort dieselben Zähler laufen.

Deshalb eine eigene Tabelle (Migrationen `core/0008_local_submission.sql` und
`indexes/0105_local_submission.sql`):

- **Eine Zeile je MBID.** Alle Zeilen einer eingereichten Aufnahme teilen sich
  `local_track_id` (Gruppenschlüssel) und `local_track_gid` (die ausgelieferte
  AcoustID). So bleibt „eine Aufnahme = ein Lookup-Treffer", während die
  Antwort je MBID eine eigene Submission-ID trägt.
- **Der Vollvektor wird mitgespeichert** (`fingerprint integer[]`, lz4). Ohne
  ihn gäbe es kein `compare2`-Rescoring, und der Lookup könnte lokale Treffer
  nur mit dem groben Index-Score bewerten.
- **Vorzeichen:** der Chromaprint-Dekoder liefert u32, die Spalte hält signed
  int32 — dasselbe Bitmuster, genau wie bei den Vektoren aus den Deltas.
- **Statusdomäne vollständig** per `CHECK`: `new`, `indexed`, `forwarded`,
  `forward_failed`. Die letzten beiden benutzt erst Phase 12; sie stehen
  bereits im Schema, damit dafür keine Migration nötig wird.

## Reservierter Dokument-ID-Bereich

Auffindbar sind lokale Einreichungen, weil sie im acoustid-index einen eigenen
Dokument-ID-Bereich belegen:

| Bereich | Inhalt |
|---|---|
| `[0, 2^31-1]` | Delta-Bestand — die Dokument-ID **ist** `fingerprint.id` (Spaltentyp `integer`). |
| `[2^31, 2^32-1]` | lokale Einreichungen — Dokument-ID = `2^31 + local_track_id`. |

**Warum genau diese Grenze.** Der Dokument-ID-Typ des Index ist in keiner
Quelle dokumentiert (der Forschungsbericht schweigt dazu, der Client nahm
bisher u64 an). Gegen das gepinnte Image gemessen (Phase 11):

| Dokument-ID | Ergebnis |
|---|---|
| `1`, `2^31-1`, `2^31`, `2^32-1` | angenommen und **unverändert** wiedergefunden |
| `2^32`, `2^32+7`, `2^40`, `2^53`, `2^64-1` | HTTP 400 `IntegerOverflow` |

Es sind also **u32**-Dokument-IDs, und zwar mit lautem Fehler statt stillem
Überlauf. `2^31` teilt den Raum exakt und nachweisbar kollisionsfrei: unterhalb
liegt alles, was `fingerprint.id` (Postgres `integer`) je annehmen kann,
oberhalb bleiben 2³¹ Plätze für eigene Einreichungen. Die Sequenz
`local_submission_track_id_seq` ist `AS integer … NO CYCLE` — sie endet bei
2147483647 und läuft nicht um; der Code prüft dieselbe Grenze ein zweites Mal.

Konsequenz für den Client: `shared/shared/fpindex/wire.py` prüft Dokument-IDs
jetzt gegen u32 statt u64 (**Korrektur eines falschen Wertebereichs im
Bestand**, Phase 5). Verankert ist der Befund in
`shared/tests/test_fpindex_integration.py`.

## Statusmaschine und Indexierung

```
new ──(_update im Index)──> indexed ──(Phase 12)──> forwarded | forward_failed
```

- **Synchron in der Anfrage**, aber in der richtigen Reihenfolge: erst das
  `_update` des Index, **dann** der Statuswechsel. Umgekehrt wäre es stiller
  Datenverlust — als indexiert vermerkte Einreichungen, die der Index nie
  gesehen hat, tauchen in keinem Lookup mehr auf (dieselbe Regel wie beim
  Index-Feed des Importers, ARCHITECTURE §5.3).
- **Warum synchron:** der API-Container hat keinen Hintergrund-Scheduler, und
  ein Weckvorgang soll nicht damit enden, dass der Stack einschläft, bevor die
  Einreichung im Index steht. Der Aufwand ist ein HTTP-Roundtrip mit fsync.
- **Index nicht erreichbar ⇒ HTTP 200 mit `pending`.** Gespeichert ist
  gespeichert; die Einreichung bleibt `new` und wird bei der nächsten
  Submit-Anfrage nachgetragen (Arbeitsvorrat = Partialindex
  `local_submission_idx_unindexed`, Resume-Denke §8.4). Ein Fehlercode wäre
  hier schädlich: Picard und beets würden dieselbe Einreichung erneut
  schicken und damit Dubletten erzeugen.
- **Nachtrag begrenzt:** höchstens 200 Einreichungen je Anfrage
  (`MAX_INDEX_BATCH`), damit ein aufgelaufener Rückstand keine einzelne
  Anfrage blockiert.
- **Ohne `expected_version`.** Der Importer sichert seine Feed-Batches
  optimistisch ab, weil er der einzige Schreiber sein soll; die API kann das
  nicht — sie schreibt neben ihm. Die Idempotenz kommt hier aus dem Inhalt:
  dieselbe Dokument-ID mit denselben Hashes zu schicken, ist folgenlos.
- **Nur Stille im Vektor:** kein Dokument, aber trotzdem `indexed` — sonst
  läge die Zeile bei jeder künftigen Anfrage wieder im Arbeitsvorrat (wie beim
  Index-Feed).

## Wirkung im Lookup

Ein Treffer aus dem reservierten Bereich wird im Store auf `local_submission`
aufgelöst:

| Zweck | Quelle |
|---|---|
| Rescoring (`compare2`) | `local_submission.fingerprint` (Vollvektor) |
| `id` der Antwort | `local_submission.local_track_gid` |
| `recordings[].id` bei `meta` | die eingereichten `mbid`-Werte |
| `sources` | Anzahl der Einreichungen derselben MBID für dieselbe Aufnahme |

Längenfenster (`duration ± maxdurationdiff`), Score-Cutoff > 0,4, Kappung auf
10 und die Deduplizierung je Track gelten unverändert. Bei Score-Gleichstand
sortiert der Lookup nach Dokument-ID aufsteigend — importierte Fingerprints
stehen deshalb vor lokalen Einreichungen. Merges (`track.new_id`) gibt es für
lokale Einreichungen nicht; sie lösen sich auf sich selbst auf. Die lokale
AcoustID lässt sich auch direkt über `trackid` nachschlagen.

## Ereignis für den Wächter

Nach jeder gespeicherten Anfrage steht eine Zeile im JSON-Log:

```json
{"event": "local_submission_stored", "submissions": 2, "recordings": 1,
 "submission_ids": [17, 18], "acoustids": ["…"], "client": "…"}
```

Der Wächter verwirft ab Phase 17 daraufhin seinen Lookup-Cache (Invariante
§8.6). **Gebaut wird das hier nicht** — nur das Ereignis entsteht.

## Grenzen

- **`usermeta` deckt lokale Einreichungen nicht ab.** Der Rückfall auf
  eingereichte Textmetadaten liest `track_meta`/`meta` aus dem Delta-Bestand;
  Textmetadaten einer lokalen Submission erscheinen deshalb in keiner
  `meta`-Antwort. Eine rein textbasierte Einreichung ist auffindbar, liefert
  aber nur AcoustID und Score.
- **Keine Zusammenführung.** Dieselbe Aufnahme zweimal eingereicht ergibt zwei
  Einreichungen mit zwei AcoustIDs und zwei Lookup-Treffern. Der
  Original-Server führt so etwas per Merge-Task zusammen (Schwelle 0,75); eine
  eigene Pflege ist bewusst nicht Teil dieses Projekts.
- **Keine Sichtprüfung gegen den Delta-Bestand.** Ein bereits bekannter
  Fingerprint wird trotzdem als lokale Einreichung gespeichert; der Lookup
  liefert dann beide AcoustIDs.
- **Keine Löschfunktion.** Zurücknehmen lässt sich eine Einreichung derzeit nur
  von Hand (Zeile löschen **und** `DELETE /:index/:id` mit
  `2^31 + local_track_id`).

## Tests

```bash
uv run pytest api/tests/test_submit_params.py api/tests/test_submit_http.py \
              api/tests/test_submit_range.py            # ohne Dienste

docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db index
AOFF_DB_HOST=127.0.0.1 AOFF_INDEX_URL=http://127.0.0.1:6081 \
  uv run pytest api/tests/test_submit_integration.py --integration=require
```

Die HTTP-Tests stellen Postgres durch eine Handvoll Zeilen im Arbeitsspeicher
nach; die Integrationstests laufen gegen echtes PG 18 und das gepinnte
Index-Image, mit **echten** Vollvektoren aus einem Tages-Delta.

## Bewusste Abweichungen vom Original

| Punkt | Original | Hier | Grund |
|---|---|---|---|
| Ablage der Einreichung | eigene Zeilen in `track`/`fingerprint`/`track_mbid` (der Server ist die Quelle) | eigene Tabelle `local_submission` + reservierter Dokument-ID-Bereich | Unser Bestand wird aus den Deltas nachgebaut; jede eigene Zeile in den Dump-Tabellen würde vom nächsten Delta überschrieben. |
| `user`-Key | wird gegen den Benutzerbestand geprüft (Fehler 6) | wird verlangt, nie geprüft | Diese Instanz hat keinen Benutzerbestand. Auth ist Sache des Wächters (§7). |
| `submit.mode` | existiert nicht | `off` ⇒ Fehler 12 / HTTP 400 | Projektentscheid ARCHITECTURE §6; Code 12 ist der einzige Code der Tabelle, der „vom Betreiber nicht erlaubt" trifft. |
| Suchindex beim Speichern nicht erreichbar | (asynchroner Worker, Frage stellt sich nicht) | HTTP 200 `pending`, Einreichung bleibt `new`, Nachtrag bei der nächsten Anfrage | Ein Fehlercode würde Clients zum erneuten Senden bringen ⇒ Dubletten. |
| Verarbeitung | asynchrone Warteschlange (Import-Skript, Merge-Tasks, Track-Zuordnung) | synchrone Indexierung, keine Merges | Ohne Worker-Prozess und ohne Pflegeaufwand; die Antwort bleibt zeichengleich `pending`. |
| Stille Verwerfung | MBID/PUID/Textmetadaten | zusätzlich zählt `foreignid` als Zuordnung | `foreignid` ist ein Identifikator wie `puid`; eine Einreichung damit zu verwerfen wäre Datenverlust. |
| Rumpf `multipart/form-data` | wird gelesen | wird ignoriert | Wie beim Lookup: kein bekannter Client benutzt es, spart eine Abhängigkeit. |
| Anzahl Teilanfragen | keine dokumentierte Grenze | ebenfalls keine (nur 1 MiB) | Picards Batching stützt sich auf das 413 der Rumpfgrenze. |

## Offene Punkte

1. **Index-Schreibkonflikt mit dem laufenden Import.** Der Index-Feed des
   Importers sichert jeden Batch mit `expected_version` ab und bricht laut ab,
   sobald ein zweiter Schreiber die Version verändert hat (DECISIONS
   „Phase-7-Import-Details", Punkt 7). Trifft ein Submit einen laufenden
   Import, kann der Import genau daran scheitern — folgenlos für die Daten
   (Resume, §8.4), aber der Lauf endet als Fehler und wird wiederholt. Für
   Phase 19 zu entscheiden: Submits während des Update-Laufs zurückstellen,
   oder den Feed ohne `expected_version` fahren.
2. **`usermeta` für lokale Einreichungen** (siehe „Grenzen") — sinnvoll
   spätestens, wenn die Instanz überwiegend eigene Einreichungen führt.
3. **Rücknahme/Bereinigung** eigener Einreichungen hat noch keine Oberfläche;
   die Admin-UI (Phasen 23–27) wäre der Ort dafür.
