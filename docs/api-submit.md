# API-Dienst: `/v2/submit` und `/v2/submission_status` (Phasen 11–13)

Referenz zum Submit-Endpunkt des **API-Prozesses** `api` und zu seiner
Statusabfrage — seit dem Ein-Container-Umbau (M1b) kein eigener Container
mehr, sondern einer der vier supervisord-Prozesse im Container
(ARCHITECTURE §3). Vertrag und Begründungen: ARCHITECTURE §5.2, §5.3, §6, §7 und
§8.9 sowie
[docs/research/phase1-api-formate.md](research/phase1-api-formate.md) und
[docs/research/phase1-acoustid-index.md](research/phase1-acoustid-index.md).
Der Lookup steht in [docs/api-lookup.md](api-lookup.md).

**Stand:** alle drei Modi vollständig — `off`, `local` (Phase 11) und
`local+upstream` samt Weiterleitung an api.acoustid.org, Statuspfaden
`forwarded`/`forward_failed`, Warteschlange und Retry-Hook (Phase 12,
Abschnitt [Upstream-Weiterleitung](#upstream-weiterleitung-localupstream)).
Seit Phase 13 beantwortet
[`/v2/submission_status`](#getpost-v2submission_status-phase-13) die vergebenen
Submission-IDs.

## Modi (`acoustid.submit.mode`, ARCHITECTURE §6)

| Modus | Verhalten |
|---|---|
| `off` | Der Endpunkt nimmt nichts an: **Fehler 12 „not allowed" / HTTP 400**, geprüft noch vor dem Lesen der Parameter. |
| `local` (Default) | Speichern in `local_submission`, indexieren, Antwort `pending`. |
| `local+upstream` | Wie `local`, **zusätzlich** Weiterleitung an api.acoustid.org. Braucht `acoustid.submit.upstream_app_key` (die Config lehnt den Modus sonst ab). |

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
  Genau diese IDs beantwortet
  [`/v2/submission_status`](#getpost-v2submission_status-phase-13).
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
  `forward_failed`. Die letzten beiden benutzt die Upstream-Weiterleitung; sie
  standen seit Phase 11 im Schema, deshalb kam Phase 12 **ohne Migration** aus
  (auch die vier Spalten `forwarded_at`, `forward_attempts`, `forward_error`
  und `submitted_by` waren schon da).

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
new ──(_update im Index)──> indexed ──(Upstream-Antwort)──> forwarded
                                    └──(Fehler)─────────> forward_failed ──┐
                                                            ↑              │
                                                            └──────────────┘
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

## Upstream-Weiterleitung (`local+upstream`)

Modul: `api/app/upstream.py`. Vertrag: ARCHITECTURE §7 („Upstream-Weiterleitung"),
§6 (`acoustid.submit.mode`, `acoustid.submit.upstream_app_key`) und Invariante §8.9.

### Was hinausgeht

Genau der Original-Submit, den auch Picard schickt — `POST` mit
`application/x-www-form-urlencoded` an **`https://api.acoustid.org/v2/submit`**:

| Feld | Wert |
|---|---|
| `client` | **unser eigener** Application-Key (`acoustid.submit.upstream_app_key`) |
| `user` | der `submitted_by`-Key des einreichenden Clients, **unverändert** |
| `clientversion` | Version dieser Instanz |
| `format` | `json` |
| `duration.0` | `local_submission.length` |
| `fingerprint.0` | aus dem gespeicherten Vollvektor **zurückgerechnet** |
| `mbid.0` | **einmal je MBID der Gruppe** (mehrfach belegter Name) |
| `bitrate.0`, `fileformat.0`, `puid.0`, `foreignid.0`, `track.0`, `artist.0`, `album.0`, `albumartist.0`, `trackno.0`, `discno.0`, `year.0` | nur, wenn gesetzt |

- **Nur `https`.** Der Original-Dienst erzwingt es nicht, hier gehen aber
  fremde `user`-Keys über die Leitung; der Weiterleiter lehnt jede andere
  Adresse schon beim Bauen ab. Die Zieladresse ist eine Konstante
  (`UPSTREAM_URL`) und in Tests über den Konstruktor ersetzbar — dasselbe
  Muster wie die `base_url` des Downloaders.
- **`user` unverändert durchreichen**, weil acoustid.org keinen Mechanismus
  für „im Namen Dritter" kennt und die Nutzungsbedingungen den User-Key an
  den Nutzer binden (Phase-1-Bericht, „Application-Key & Nutzungsregeln").
  Der Key wird deshalb auch **nie geloggt**.
- **Der Fingerprint wird neu kodiert.** Gespeichert ist der Vollvektor, nicht
  die Zeichenkette des Clients; `encode_fingerprint` ist das verlustfreie
  Gegenstück zum Dekoder (bit-verifiziert in CI). Eine zusätzliche Spalte für
  die Original-Zeichenkette wäre eine Migration ohne Gegenwert.
- **Eine Anfrage je Einreichungsgruppe** (`local_track_id`): eine Aufnahme mit
  drei MBIDs steht als drei Zeilen in der Tabelle und geht als **eine**
  Anfrage mit dreifachem `mbid.0` hinaus — so, wie der Client sie eingereicht
  hat. Mehrere Gruppen werden **nicht** gebündelt: sie können verschiedene
  `user`-Keys tragen (davon gibt es je Anfrage nur einen), und eine Anfrage je
  Gruppe hält Erfolg und Fehlschlag eindeutig der Gruppe zugeordnet, deren
  Status danach umgesetzt wird.

### Wann weitergeleitet wird

Zwei Wege — ein Hintergrund-Worker existiert bewusst nicht (er kollidierte
mit dem Schlaf-Zyklus, DECISIONS „Phase-11-Submit-Details"):

| Weg | Auslöser | Umfang | HTTP-Versuche je Gruppe |
|---|---|---|---|
| `forward_after_submit` | die Submit-Anfrage selbst, nach Speichern + Indexieren | **nur die Gruppen dieser Anfrage**, höchstens 10 | 1 |
| `drain_queue` | Wächter im Update-Zyklus (Phase 19), Admin-UI (Phase 26) | ganzer Arbeitsvorrat, höchstens 500 Gruppen | 5 |

Der erste Versuch gehört in die Anfrage, weil §8.9 von Fehlversuchen spricht,
die „beim nächsten Update-Lauf **erneut** versucht" werden — und weil der
Stack sonst einschlafen könnte, bevor überhaupt etwas hinausgegangen ist. Die
Deckelung auf 10 Gruppen hält die Antwortzeit bei ≤ 3 Anfragen/s unter rund
3,5 Sekunden; der Rest bleibt `indexed` und geht im nächsten
Warteschlangenlauf hinaus. Im Anfragepfad gibt es **kein** Backoff — bis zu
30 Sekunden Wartezeit in einer offenen Client-Anfrage wären eine Zumutung.

**Weitergeleitet wird nur, was `indexed` ist.** Der Status ist eine einzige
Spalte; eine Einreichung, die der Suchindex noch nicht kennt, darf diese
Information nicht gegen `forwarded` eintauschen. Zeilen im Status `new` holt
der nächste Submit nach und landen erst danach in der Warteschlange.

### Drossel und Backoff

- **Hart ≤ 3 Anfragen/s** (Nutzungsbedingung von acoustid.org): ein
  Mindestabstand von ⅓ s zwischen zwei Anfragen, durchgesetzt von einer
  **prozessweiten** Drossel mit Schloss und monotoner Uhr. Prozessweit ist
  wesentlich: die API bearbeitet Anfragen im Threadpool, und ein
  Warteschlangenlauf kann parallel dazu laufen. Deshalb hängt der
  Weiterleiter am `ApiService` und nicht an der Anfrage. Der Zeitpunkt wird
  unter dem Schloss reserviert, gewartet wird ausserhalb.
- **Backoff exponentiell 1 s → 2 → 4 → 8 → 16 → 30 s (Deckel).** Upstream
  schickt kein `Retry-After`, also ein eigenes Schema. Es greift nur zwischen
  den HTTP-Versuchen **eines** Laufs.
- **Zwei Fehlerklassen.** Netzfehler, Zeitüberschreitung, 408/429/5xx und
  unlesbare Antworten heissen „der Dienst ist gerade nicht da": der Lauf
  bricht danach ab, die restlichen Gruppen behalten ihren Zähler
  (`skipped` im Bericht). Ein 4xx oder eine Fehlerantwort im AcoustID-Format
  betrifft nur diese eine Gruppe; der Lauf macht weiter.

### Warteschlange und die 7-Fehler-Grenze (§8.9)

Arbeitsvorrat sind alle Gruppen mit `status = 'indexed'` **oder**
`status = 'forward_failed' AND forward_attempts < 7`, älteste zuerst.

- Erfolg ⇒ `status = 'forwarded'`, `forwarded_at = now()`,
  `forward_error = NULL`. `forward_attempts` bleibt stehen — die Zahl ist
  Historie und wird nicht geschönt.
- Fehlschlag ⇒ `status = 'forward_failed'`, `forward_attempts + 1`,
  `forward_error` = gekürzte Ursache (max. 500 Zeichen).
- **`forward_attempts` zählt Läufe, nicht HTTP-Versuche.** Ein
  Warteschlangenlauf mit fünf HTTP-Versuchen ist ein Fehlversuch.
- Ab dem **7.** Fehlversuch fällt die Gruppe aus dem Arbeitsvorrat: kein
  automatischer Versuch mehr, und ein strukturiertes Ereignis geht ins Log
  (Abnehmer: Benachrichtigung „Upstream-Submit dauerhaft fehlgeschlagen",
  Phase 20).
- Statuswechsel nur aus einem gültigen Vorzustand — die `UPDATE`s tragen
  `AND status IN ('indexed', 'forward_failed')`. Eine bereits weitergeleitete
  Gruppe wechselt kein zweites Mal.
- Eine Zeile ohne `submitted_by` scheitert **ohne** Anfrage („kein user-Key
  hinterlegt"): raten wäre eine Zweckentfremdung eines fremden Keys. Über den
  Endpunkt kann das nicht entstehen (`user` ist Pflicht), die Spalte ist aber
  NULL-bar.

### Manueller Wiederholungsversuch

`retry_forward(connection, service, local_track_ids=None)` ist der Hook aus
§8.9 — aufrufbar für die Trigger-API des Wächters (Phase 19) und den Knopf
„Upstream-Queue senden" der Admin-UI (Phase 26):

- **ohne Namensnennung:** setzt alle Gruppen zurück, die die Grenze erreicht
  haben (`forward_attempts >= 7` ⇒ 0, `forward_error` geleert), und versucht
  **genau diese** erneut. Gruppen unterhalb der Grenze bleiben unangetastet —
  sie kommen beim nächsten Lauf ohnehin dran.
- **mit `local_track_ids`:** setzt genau diese zurück (ab dem ersten
  Fehlversuch) und versucht sie erneut.

### Ereignisse im Log

```json
{"event": "upstream_submission_forwarded", "local_track_id": 17, "mbids": 2,
 "http_attempts": 1, "upstream_submission_ids": [4711, 4712]}
{"event": "upstream_forward_gave_up", "local_track_id": 17,
 "forward_attempts": 7, "max_forward_attempts": 7, "forward_error": "…"}
```

Weder der Application-Key noch der `user`-Key erscheinen darin — der
Application-Key wird aus jeder Fehlermeldung entfernt (`***`), bevor sie ins
Log oder in `forward_error` geht.

### Die Submission-IDs des Originals werden nicht gespeichert

Die Antwort von api.acoustid.org enthält eigene Submission-IDs. Sie landen
**nur im Log** (`upstream_submission_ids`), nicht in der Datenbank:

- Sie in eine Spalte neben die lokalen IDs zu legen, wäre ein Datenmodellfehler
  — `/v2/submission_status` beantwortet ausschliesslich **lokale** IDs, und ein
  vermischter Bestand wäre nicht mehr auseinanderzuhalten.
- Eine eigene Spalte wäre eine Migration ohne Abnehmer: die IDs sind nur gegen
  das `/v2/submission_status` des Originals etwas wert, und diese Instanz
  fragt dort nichts nach.
- Nachvollziehbar bleiben sie über das Log-Ereignis.

## `GET/POST /v2/submission_status` (Phase 13)

Der kleine Bruder des Submit: wer eingereicht hat, bekam Submission-IDs und
die Auskunft `pending`; hier fragt er später nach. Der Endpunkt heißt
**`/v2/submission_status`** — nicht `/v2/submit/status`; das ist die
Handoff-Korrektur aus der Phase-1-Recherche (ARCHITECTURE §7,
Kompatibilitätsvertrag).

`GET` und `POST` sind gleichwertig; Parameter aus Query-String **und**
Formular-Rumpf, gzip und die 1-MiB-Grenze wie überall.

| Name | Pflicht | Bedeutung |
|---|---|---|
| `client` | ja | Application-Key; nur auf Anwesenheit geprüft. |
| `id` | ja | Submission-ID aus der Submit-Antwort. **Mehrfach erlaubt**, höchstens 100 je Anfrage. |
| `format` | nein | `json` (Default), `jsonp`, `xml`. |
| `jsoncallback` | nein | Funktionsname für `jsonp`. |
| `clientversion` | nein | Nur fürs Log. |

Ein `user` kommt hier **nicht** vor — der Endpunkt gehört zum Submit, kennt
aber keinen Benutzer (Phase-1-Bericht).

### Antwort

Je angefragter ID ein Eintrag, in Anfragereihenfolge:

```json
{"status": "ok", "submissions": [
  {"id": 17, "status": "imported", "result": {"id": "<acoustid>"}},
  {"id": 18, "status": "pending"}
]}
```

`result` steht nur bei `imported` und trägt die AcoustID der Einreichung
(`local_submission.local_track_gid`) — dieselbe UUID, die der Lookup
ausliefert.

### Abbildung auf die Statusmaschine

`local_submission.status` hat vier Werte (ARCHITECTURE §5.2), die Antwort
kennt zwei:

| Status in der DB | Antwort | Warum |
|---|---|---|
| `new` | `pending` | Gespeichert, aber der Suchindex kennt sie noch nicht — sie ist **noch nicht auffindbar**. Genau das heißt „wird noch verarbeitet". |
| `indexed` | `imported` + `result.id` | Ab hier liefert der Lookup sie aus. Das ist lokal exakt das, was `imported` upstream bedeutet: die Einreichung hat eine AcoustID und ist nachschlagbar. |
| `forwarded` | `imported` + `result.id` | Wie `indexed`; die Weiterleitung ändert am lokalen Ergebnis nichts. |
| `forward_failed` | `imported` + `result.id` | Ebenfalls: die Einreichung ist **lokal** fertig und auffindbar. Nur der Weg nach api.acoustid.org scheiterte — eine Sache des Betreibers (Warteschlange, §8.9), nicht des Clients. Sie hier `pending` zu nennen, ließe Clients ewig weiterfragen, obwohl alles erledigt ist. |
| unbekannte / fremde ID | `pending` | Vertrag des Originals: **nie 404**. Eine Instanz, die auf jede fremde ID mit 404 antwortet, verrät zugleich, welche IDs es gibt. |

**Beantwortet werden ausschließlich lokale IDs.** Die Submission-IDs, die
api.acoustid.org bei der Weiterleitung vergibt, stehen nur im Log und nicht in
der Datenbank (siehe [unten](#die-submission-ids-des-originals-werden-nicht-gespeichert));
ein vermischter Bestand wäre nicht auseinanderzuhalten.

### Entscheidungen und Abweichungen

| Punkt | Festlegung | Grund |
|---|---|---|
| `id`-Obergrenze | **100** je Anfrage ⇒ Fehler 19 / HTTP 413 | Das Original nennt keine Grenze; ohne eine wäre der Endpunkt ein billiger Verstärker (eine Anfrage, beliebig viele Antwortzeilen). 100 ist derselbe Wert wie das Track-Query-Limit des Lookups und das Batch-Limit — und für echte Clients folgenlos: Picard und beets benutzen den Endpunkt gar nicht. Gezählt werden die **geschickten** Werte, nicht die lesbaren: die Grenze ist Missbrauchsschutz, kein Qualitätsurteil. |
| Reihenfolge und Wiederholungen | Antwort in Anfragereihenfolge, ein Eintrag **je angefragtem Wert** (auch doppelt) | Beantwortet wird, was gefragt wurde; das ist die vorhersagbarste Zuordnung für den Client. Die Datenbank wird trotzdem nur **einmal** befragt (entdoppelte ID-Liste, ein `SELECT`). |
| Unlesbare oder nicht-positive `id` | still übersprungen | Die weiche Zahlenlesart dieser API (`duration=abc` gilt ebenfalls als „nicht angegeben"). Bleibt danach keine ID übrig, ist das derselbe Fall wie „gar keine geschickt": Fehler 2. |
| `id.N`-Suffixe | **nicht** unterstützt | Der Phase-1-Bericht nennt für diesen Endpunkt ausdrücklich nur mehrfaches `id`; ein Suffixprotokoll hier zu erfinden wäre unbelegt. |
| `acoustid.submit.mode = off` | Der Endpunkt antwortet trotzdem | Er liest nur. Wer den Submit abschaltet, soll weiterhin erfahren, was aus früheren Einreichungen geworden ist. |
| `forward_failed` ⇒ `imported` | siehe Tabelle oben | Der Client fragt nach **seiner** Einreichung, nicht nach dem Betriebszustand der Upstream-Queue. |
| Fehler 18 („fingerprint not found") | wird hier nie erzeugt | Unbekannte IDs bleiben `pending` — Vertrag des Originals. |

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
- **Keine Rücknahme upstream.** Was einmal weitergeleitet ist, lässt sich von
  hier aus nicht zurückholen; der Original-Dienst kennt keinen solchen Aufruf.
- **Kein Nachfragen bei `/v2/submission_status` des Originals.** Ob eine
  weitergeleitete Einreichung dort tatsächlich verarbeitet wurde, prüft diese
  Instanz nicht. `forwarded` heisst „angenommen", nicht „importiert".

## Tests

```bash
uv run pytest api/tests/test_submit_params.py api/tests/test_submit_http.py \
              api/tests/test_submit_range.py api/tests/test_upstream.py \
              api/tests/test_status_http.py                              # ohne Dienste

docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db index
MMO_DB_HOST=127.0.0.1 MMO_INDEX_URL=http://127.0.0.1:6081 \
  uv run pytest api/tests/test_submit_integration.py --integration=require
```

Die HTTP-Tests stellen Postgres durch eine Handvoll Zeilen im Arbeitsspeicher
nach (`api/tests/stubs.py`, `FakeDb`); die Integrationstests laufen gegen
echtes PG 18 und das gepinnte Index-Image, mit **echten** Vollvektoren aus
einem Tages-Delta.

**An api.acoustid.org geht in keinem Test etwas hinaus** — auch nicht mit
`--network`. An seiner Stelle steht ein `httpx.MockTransport`
(`api/tests/upstream_mock.py`); der Weiterleiter selbst ist der echte, damit
das Wire-Format mitgeprüft wird. Drossel und Backoff bekommen eine Testuhr,
die nur durch das eigene `sleep` weiterläuft — die Wartezeiten sind als
Zahlenreihe einklagbar, gewartet wird nie.

## Bewusste Abweichungen vom Original

| Punkt | Original | Hier | Grund |
|---|---|---|---|
| Ablage der Einreichung | eigene Zeilen in `track`/`fingerprint`/`track_mbid` (der Server ist die Quelle) | eigene Tabelle `local_submission` + reservierter Dokument-ID-Bereich | Unser Bestand wird aus den Deltas nachgebaut; jede eigene Zeile in den Dump-Tabellen würde vom nächsten Delta überschrieben. |
| `user`-Key | wird gegen den Benutzerbestand geprüft (Fehler 6) | wird verlangt, nie geprüft | Diese Instanz hat keinen Benutzerbestand. Auth ist Sache des Wächters (§7). |
| `acoustid.submit.mode` | existiert nicht | `off` ⇒ Fehler 12 / HTTP 400 | Projektentscheid ARCHITECTURE §6; Code 12 ist der einzige Code der Tabelle, der „vom Betreiber nicht erlaubt" trifft. |
| Suchindex beim Speichern nicht erreichbar | (asynchroner Worker, Frage stellt sich nicht) | HTTP 200 `pending`, Einreichung bleibt `new`, Nachtrag bei der nächsten Anfrage | Ein Fehlercode würde Clients zum erneuten Senden bringen ⇒ Dubletten. |
| Verarbeitung | asynchrone Warteschlange (Import-Skript, Merge-Tasks, Track-Zuordnung) | synchrone Indexierung, keine Merges | Ohne Worker-Prozess und ohne Pflegeaufwand; die Antwort bleibt zeichengleich `pending`. |
| Stille Verwerfung | MBID/PUID/Textmetadaten | zusätzlich zählt `foreignid` als Zuordnung | `foreignid` ist ein Identifikator wie `puid`; eine Einreichung damit zu verwerfen wäre Datenverlust. |
| Rumpf `multipart/form-data` | wird gelesen | wird ignoriert | Wie beim Lookup: kein bekannter Client benutzt es, spart eine Abhängigkeit. |
| Anzahl Teilanfragen | keine dokumentierte Grenze | ebenfalls keine (nur 1 MiB) | Picards Batching stützt sich auf das 413 der Rumpfgrenze. |
| Weiterleitung (Phase 12) | existiert nicht (der Dienst **ist** das Ziel) | Modus `local+upstream` reicht jede Einreichung an api.acoustid.org weiter | Projektentscheid ARCHITECTURE §6/§7: eine Offline-Instanz soll den Bestand nicht versanden lassen. |
| `client` der weitergeleiteten Anfrage | der Key des einreichenden Clients | **unser eigener** Application-Key | Wir sind gegenüber acoustid.org der Aufrufer; der Key des Clients gehört nicht uns. |
| `user` der weitergeleiteten Anfrage | — | der `user`-Key des Clients, unverändert | acoustid.org kennt kein „im Namen Dritter"; die Nutzungsbedingungen binden den User-Key an den Nutzer. |
| Fingerprint der weitergeleiteten Anfrage | (die Original-Zeichenkette liegt vor) | aus dem Vollvektor neu kodiert | Gespeichert ist der Vektor; der Codec ist verlustfrei und bit-verifiziert — eine Spalte für die Zeichenkette wäre eine Migration ohne Gegenwert. |
| Bündelung upstream | ein Client bündelt viele Aufnahmen je Anfrage | **eine** Anfrage je Einreichungsgruppe | Verschiedene Gruppen können verschiedene `user`-Keys tragen; je Gruppe eine Anfrage hält die Statuszuordnung eindeutig. |
| Upstream-Submission-IDs | (die eigenen IDs sind die Antwort) | nur im Log, nicht in der Datenbank | Kein Vermischen mit den lokalen IDs, die `/v2/submission_status` beantwortet; eine eigene Spalte hätte keinen Abnehmer. |
| Fehler bei der Weiterleitung | — | HTTP 200 `pending`, Zeile wird `forward_failed` | Lokal gespeichert ist die Wahrheit; ein Fehlercode brächte Clients zum erneuten Senden ⇒ Dubletten. |
| `/v2/submission_status`: `imported` (Phase 13) | „von der Warteschlange des Servers verarbeitet" | „bei uns indexiert" (`indexed`, `forwarded`, `forward_failed`) | Lokal ist genau das der Moment, ab dem der Lookup die Einreichung ausliefert — dieselbe Zusage in unserer Architektur. |
| `/v2/submission_status`: Anzahl `id` | keine dokumentierte Grenze | höchstens 100 ⇒ Fehler 19 / HTTP 413 | Missbrauchsschutz; derselbe Wert wie Track-Query- und Batch-Limit, für echte Clients folgenlos. |

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
4. **Der Weiterleiter ist nie gegen den echten Dienst gelaufen.** Format und
   Nutzungsregeln stammen aus dem Quelltext-Studium der Phase 1, geprüft wird
   gegen eine Attrappe. Ein erster echter Lauf braucht einen registrierten
   Application-Key (acoustid.org/new-application, sofort aktiv) und sollte mit
   **einer** Einreichung beginnen; unbestätigt bleiben bis dahin die genaue
   Fehlerantwort bei ungültigem Key und das Verhalten bei Überschreiten der
   3-Anfragen-Grenze.
5. **Modus-Wechsel `local` → `local+upstream` schiebt den Altbestand nach.**
   Der erste Warteschlangenlauf danach nimmt **alle** bisher nur lokal
   gespeicherten Einreichungen mit (höchstens 500 je Lauf, ≤ 3 Anfragen/s).
   Das ist gewollt, sollte in der Admin-UI (Phase 25) aber am Umschalter
   stehen.
6. **Kein Abgleich mit `/v2/submission_status` des Originals**: `forwarded`
   heisst „angenommen", nicht „importiert" (siehe „Grenzen"). Ein Nachfragen
   wäre erst mit einer eigenen Spalte für die Upstream-IDs sinnvoll.
