# DECISIONS.md — acoustid-offline

Entscheidungslog. Neue Einträge oben anfügen. Format:
`## YYYY-MM-DD: Titel` / Entscheidung / Begründung / Alternativen.

---

## 2026-07-25: Code-Lizenz MIT

Entscheidung: Der eigene Code des öffentlichen Repos steht unter MIT
(Copyright „acoustid-offline contributors").
Begründung: Maximal einfach und kompatibel; üblich im AcoustID-Umfeld
(acoustid-server ist MIT). acoustid-index (GPL-3.0) läuft nur als
separater HTTP-Dienst und infiziert den eigenen Code nicht.
Alternativen: GPL-3.0, AGPL-3.0 — verworfen (Betreiber-Entscheid).

## 2026-07-25: Echte Dump-Fixtures nicht im öffentlichen Repo

Entscheidung: Die Test-Fixtures aus echten data.acoustid.org-Dateien
(tests/fixtures/acoustid-dumps/) werden nicht committet (.gitignore);
stattdessen liegt ein Fetch-Skript bei, das exakt dieselben 9 Dateien
reproduzierbar lädt (auch für CI nutzbar).
Begründung: Handoff-Scope „keine Weiterverteilung des Datenbestands";
ein öffentliches Repo mit echten Datenauszügen wäre genau das.
Nebenwirkung: Fixtures bleiben aktuell beschaffbar, solange die Quelle
die Historie vorhält (tut sie seit 2011 lückenlos).
Alternativen: Committen mit CC-BY-SA-Attribution (lizenzrechtlich
vertretbar, aber gegen den Handoff-Scope), synthetische Fixtures
(verlieren Beweiskraft gegen das echte Format) — verworfen.

## 2026-07-25: acoustid-index auf dem SSD-Cache-Pool

Entscheidung: Die Indexdaten (~40–55 GB erwartet, Volume ~70 GB) liegen
auf dem SSD-Cache-Pool (Unraid-Share „Prefer/Only: Cache"), nicht auf
dem Array (Betreiber-Entscheid auf Basis der Phase-1-Messdaten; löst
die vertagte Entscheidung vom selben Tag auf).
Begründung: Der Index lädt bei jedem Start seine kompletten Daten
(MAP_POPULATE) und braucht sie dauerhaft im Page-Cache; kalte Suchen
auf HDD dauern 40–80 s (= Timeout/HTTP 500), auf SSD ~1 s, warm ms.
Alternativen: Array (unbrauchbar laut Messdaten), erst Messlauf
(unnötig — die Größenordnung ist gesichert; Feinwerte liefert der
Phase-8-Probelauf).

## 2026-07-25: Rescoring per Python-Nachbau mit CI-Bit-Verifikation

Entscheidung: AcoustID-kompatible Scores entstehen zweistufig:
Index-Kandidaten (limit 20–40) → Rescoring in Python (Nachbau
`acoustid_compare2`, max_offset 80, Längenfilter ±7, Cutoff >0,4)
gegen den Vollvektor aus Postgres. Das offizielle Postgres-Image
bleibt (wie im Handoff). CI verifiziert `extract_query` und `compare2`
bit-genau gegen die Original-C-Extension, die dafür ausschließlich als
Test-Container läuft.
Begründung: pg_acoustid hat keine Lizenz (nicht weiterverbreiten) und
bräuchte ein Custom-PG-Image; ~50–150 ms Python-Rescoring je Lookup
sind für eine Privatinstanz unkritisch. Die offizielle
Python-Referenz von `extract_query` ist defekt — deshalb die
Bit-Verifikation gegen die autoritative C-Implementierung.
Alternativen: `ghcr.io/acoustid/postgres` als DB-Image (Abweichung vom
Handoff, Wartungsstand ungewiss) und eigenes PG-Image mit Extension
(Weiterverbreitung unlizenzierten Codes) — beide verworfen
(Betreiber-Entscheid).

## 2026-07-25: Query-Hash-Anzahl als Konfigurationswert

Entscheidung: Die Anzahl der indexierten Query-Hashes je Fingerprint
ist konfigurierbar (`index.query_hashes`, Default 120; z. B. 80 für
RAM-knappe Hosts). Der Phase-8-Probelauf liefert die Messwerte für
eine RAM-/Größen-Empfehlungstabelle in der Doku. Änderung des Werts
erfordert einen Index-Neuaufbau (dokumentieren).
Begründung: Betreiber-Vorgabe „erst messen und einstellen lassen, da
andere Systeme andere RAM-Größen haben" — das Projekt soll auf
beliebigen Docker-Hosts laufen, nicht nur auf dem Referenz-Server.
Alternativen: fester Wert 120 oder 80 — verworfen (Host-abhängig).

## 2026-07-25: Index-Image per Digest gepinnt; `ng` beobachten

Entscheidung: Es wird `ghcr.io/acoustid/acoustid-index` (Zig-`main`)
verwendet, per Image-Digest gepinnt. Der Nachfolger `ng` wird
beobachtet, nicht abgewartet.
Begründung: Kein Docker-Hub-Image, kein Release-Prozess, `main` ist
ein bewegliches Tag → Digest-Pin. `ng` hat kein Image und kein
Release-Datum, ist aber wire-kompatibel — unsere Integration überlebt
den Umstieg, nur der Index wäre neu zu befüllen.
Alternativen: `latest` (älterer Stand v25.4.0), `stable` (C++ 2022),
auf `ng` warten — verworfen.

## 2026-07-25: apikey-Modus — Whitelist-Schalter für Drittclient-Keys

Entscheidung: Der `apikey`-Modus akzeptiert die fest einkodierten,
öffentlich bekannten Keys von Drittclients (Picard `v8pQ6oyB`, beets
`1vOwZtEn`) nur, wenn `auth.allow_known_client_keys` aktiv ist —
**Default aus** (Betreiber-Entscheid).
Begründung: Eine ständige Whitelist würde bei Exponierung jedem
Zugriff geben, der die öffentlichen Keys kennt; im LAN-Normalfall
(auth `none`) sind Drittclients ohnehin nicht betroffen.
Alternativen: immer zulassen (schwächt Exponierungsschutz), nie
zulassen (sperrt Picard/beets im apikey-Modus komplett aus) —
verworfen.

## 2026-07-25: API-Kompatibilitätsvertrag nach Code-Recherche fixiert

Entscheidung: Die Kompatibilitäts-Anforderungen aus der
Quellcode-Recherche sind verbindlich (ARCHITECTURE §7 +
docs/research/phase1-api-formate.md): GET+POST, form-encoded +
gzip-Bodys, 1-MiB→413, Chromaprint-Base64-Decoder, meta-Präzedenz,
`sources` für Picard, Score-Semantik (>0,4/max 10/dedupliziert),
19 Original-Fehlercodes mit HTTP-Mapping, CORS. Korrektur zum
Handoff: `/v2/submission_status` statt `/v2/submit/status`.
Zusätzlich zum eigenen `/v2/lookup/batch` (100) wird das
Original-Batchprotokoll (`fingerprint.N` + `batch=1`, max 20)
unterstützt. Upstream: eigener Application-Key, `user`-Key
durchreichen, ≤3 req/s, eigenes Backoff, https.
Begründung: Reale Clients (Picard/beets) hängen an exakt diesen
Details (413-Batching, sources-Ranking, Base64-Variante).
Alternativen: nur Doku-Stand implementieren — verworfen (Doku ist
nachweislich unvollständig/teils falsch).

## 2026-07-25: MB-Query-Schicht — Raw-SQL, Online-Redirects, RO-Rolle

Entscheidung: Die MB-Anbindung folgt dem Phase-1-Entwurf
(docs/research/phase1-mb-schema.md): Raw-SQL statt mbdata-Paket; 10
Batch-Funktionen in genau einer Datei; Selfcheck + Schema-Guard beim
Start; Circuit-Breaker + eigene Exceptions für degradierten Betrieb;
Redirect-Auflösung für gemergte Recordings online bei Misses, Antwort
mit kanonischer MBID (Flag für Durchreichung); Read-only-Rolle
`acoustid_ro` per dokumentiertem SQL-Snippet (Betreiber führt es
einmalig aus); Dauer-Sekunden per Integer-Division abgeschnitten
(bit-kompatibel zum Original).
Begründung: Kapselung macht die jährliche MB-Schema-Änderung (bisher
nur additiv) zum Nicht-Ereignis; ohne Redirect-Auflösung lieferten
alternde track_mbid-Daten dauerhaft leere Metadaten; mbdata wäre eine
7000-Zeilen-Abhängigkeit mit eigener Schema-Kopplung.
Alternativen: mbdata-Modelle, FDW (bereits früher verworfen),
periodischer track_mbid-Rewrite statt Online-Auflösung (als spätere
Optimierung offen) — verworfen bzw. vertagt.

## 2026-07-25: Bootstrap per Voll-Replay aller Tagesdeltas, alle 7 Ströme

Entscheidung: Der Bootstrap spielt alle Tagesdeltas seit 2011-08-19 ab
(Stand heute: 5.454 Tage, 38.178 Dateien, 414 GB gz) — zur Laufzeit als
resumierbarer Importer-Job, niemals in Images gebündelt. Auf
Betreiber-Entscheid werden **alle 7 Ströme** geladen und importiert
(inkl. Usermeta `meta`/`track_meta`, ~19 GB extra; `track_puid` läuft
in der Lückenprüfung mit). Vor dem Vollimport ist ein zeitlich
begrenzter **Probelauf mit Messung** (Dauer, DB-/Index-Größe,
Hochrechnung) Pflicht — Phase 8.
Begründung: Es existiert kein Voll-Snapshot (`ExportTableFull`
unimplementiert; Alt-Dumps seit 2019 aufgegeben, 2021 angekündigte
Aggregate nie geliefert). Nirgends ist eine E2E-Importdauer belegt —
ohne Probelauf wäre der Vollimport ein Blindflug.
Alternativen: Nur Kern-Ströme (−19 GB) — vom Betreiber verworfen;
zeitlich beschnittener Korpus (z. B. letzte 5 Jahre) — verworfen
(unvollständiger Bestand = schlechtere Trefferquote).
Akzeptierte Lücke: Zeilen von vor 2011-08-19 ohne spätere Änderung
fehlen prinzipbedingt (Obergrenze ~10 % des Bestands).

## 2026-07-25: Import-Verfahren — direkter JSONL-Parse mit Batch-Upserts

Entscheidung: Zeilenweiser JSON-Parse + Batch-Upserts
(`ON CONFLICT (id) DO UPDATE`), Absent⇒NULL/false-Regel, `disabled`
explizit zurücksetzen; beim Bootstrap Sekundärindizes/FKs erst nach dem
Massenimport; Download und Import entkoppelt (Prefetch) mit Resume auf
beiden Ebenen (HTTP-Range; `import_state` je Strom+Tag). KEIN
COPY-FROM-Staging.
Begründung: Die Dateien sind valides JSONL — die COPY-Escaping-Hypothese
aus der Code-Analyse wurde an den Fixtures empirisch widerlegt (52
Quote-Werte parsen sauber); ein COPY-FROM-Textimport würde die Dateien
sogar korrumpieren. Bulk-Muster (Indizes nachziehen, Batches, Prefetch,
Resume) sind durch Prior Art belegt (chromaforge Apache-2.0, offizielle
populate-Skripte, dokumentierte CDN-Abbrüche).
Alternativen: COPY-Staging (verworfen, s. o.); Einzel-INSERTs
(verworfen, zu langsam für 100+ Mio. Zeilen).

## 2026-07-25: Fingerprint-Vektoren in Postgres, Index erhält nur Query-Extrakte

Entscheidung: Die vollen signed-int32-Vektoren liegen in
`fingerprint.fingerprint` (Postgres). Der acoustid-index erhält je
Fingerprint nur den extrahierten Query (Offset 80, max. 120 Hashes,
28-Bit-Maske, Silence-Hash gefiltert, unsigned) — als Python-Nachbau
von `acoustid_extract_query`. Die pg_acoustid-Extension wird nicht
eingesetzt; das Rescoring der Index-Kandidaten passiert außerhalb der
DB (Detail-Festlegung in Phase 1).
Begründung: Vollvektoren im Index bedeuten dokumentiert ~50 s statt
~50 ms pro Query (Aussage des AcoustID-Autors). pg_acoustid hat keine
Lizenzdatei und bräuchte ein Custom-Postgres-Image — das Handoff setzt
das offizielle Image. Der Extraktions-Algorithmus ist vollständig
bekannt und trivial nachbaubar.
Alternativen: pg_acoustid einsetzen (verworfen: Lizenz ungeklärt,
Custom-Image nötig); Vollvektoren in den Index (verworfen: Performance).
Hinweis: Präzisiert den Handoff-Wortlaut („Fingerprint-Vektoren leben im
acoustid-index") — der Index hält Extrakte, die Vollvektoren Postgres.

## 2026-07-25: Platzierung des acoustid-index vertagt bis nach Phase 1

Entscheidung: Ob der acoustid-index auf dem Array (Handoff-Annahme) oder
dem SSD-Cache-Pool liegt, wird erst nach den Phase-1-Kennzahlen der
aktuellen Index-Version entschieden (Betreiber-Entscheid auf Rückfrage).
Begründung: Alle Erfahrungswerte sprechen gegen HDD (Index muss
RAM-gecacht/SSD-nah sein; ~41–49 GB geschätzt) — aber die Zahlen stammen
von 2015 bzw. aus Fremdprojekten und gelten nicht verifiziert für die
aktuelle Zig-Implementierung.
Alternativen: Sofort Cache (Empfehlung der Recherche) oder sofort Array —
beide zurückgestellt bis zur Messung.

## 2026-07-25: Auth-Prüfung und Rate-Limit werden im Wächter durchgesetzt

Entscheidung: API-Key-Prüfung (`apikey`-Modus) und das IP-Rate-Limit
setzt der Wächter am Proxy durch — nicht der API-Service. Gilt auch für
Cache-Hits bei schlafendem Stack. (Rückfrage an den Auftraggeber,
entschieden 2026-07-25.)
Begründung: Die Key-Liste liegt in der Wächter-SQLite, und Cache-Hits
müssen geprüft werden können, ohne das Array zu wecken — das kann nur
der Wächter.
Alternativen: Prüfung im API-Service (bräuchte Key-Sync und ließe
Cache-Hits ungeprüft) oder doppelte Durchsetzung (Mehraufwand ohne
klaren Mehrwert im LAN) — beide verworfen.

## 2026-07-25: Steuerungsdateien kommen mit ins öffentliche Repo

Entscheidung: ARCHITECTURE.md, PROGRESS.md, DECISIONS.md und
LEARNINGS.md werden im öffentlichen GitHub-Repo geführt (ab Phase 2).
Begründung: Transparente, versionierte Projektsteuerung; die Dateien
enthalten keine Secrets.
Alternativen: Nur ARCHITECTURE.md öffentlich oder alle lokal —
verworfen (Betreiber-Entscheid auf Rückfrage).

## 2026-07-25: Admin-UI-Design bleibt vollständig bei der Design-Session

Entscheidung: Alles Designbezogene (visuelle Richtung, Navigation,
Chart-Lösung, Speicher-Interaktion, Badge-Ausgestaltung) wird
zurückgestellt, bis die separate Claude-Design-Session auf Basis von
docs/DESIGN_HANDOFF.md geliefert hat; die UI-Phasen 23–27 sind bis
dahin blockiert und nehmen keine Design-Entscheidungen vorweg.
Begründung: Design entsteht laut Handoff in der Design-Session; doppelte
oder vorweggenommene Entscheidungen würden Rework erzeugen.
Alternativen: UI parallel „nach Gefühl" bauen und später anpassen —
verworfen (Betreiber-Vorgabe 2026-07-25).

## 2026-07-25: Eigener schlanker API-Layer statt offiziellem acoustid-server

Entscheidung: `/v2/lookup`, `/v2/submit` und der Batch-Endpoint werden als
eigener FastAPI-Service implementiert; der offizielle acoustid-server wird
nicht deployt.
Begründung: Weniger Ballast; Dump-Import, MB-Direktanbindung und die
Modi-Schalter (auth/submit) wären beim offiziellen Server ohnehin
Sonderwege.
Alternativen: Offiziellen acoustid-server betreiben und anpassen —
verworfen wegen Anpassungsaufwand und unnötigem Funktionsumfang.

## 2026-07-25: acoustid-index als Matching-Kern

Entscheidung: Der Fingerprint-Suchindex ist das offizielle
acoustid-index-Image; die Fingerprint-Vektoren leben ausschließlich dort.
Begründung: Erprobter Suchkern; Eigenbau des Matchings wäre das größte
vermeidbare Risiko.
Alternativen: Eigene Matching-Implementierung (z. B. in Postgres) —
verworfen; acoustid-index bleibt gesetzt. Offene Detailfragen (Version,
API, Rebuild-Kosten) → Phase 1.

## 2026-07-25: MB-Metadaten per direktem Read-only-DB-Zugriff

Entscheidung: Der API-Service fragt die MusicBrainz-Postgres des
vorhandenen musicbrainz-docker-Stacks direkt read-only ab (gekapselte
Query-Schicht, `mb.dsn`).
Begründung: Entkoppelter und einfacher zu debuggen als eine
DB-zu-DB-Kopplung; bei MB-Ausfall degradierter Betrieb statt Fehler.
Alternativen: Foreign Data Wrapper in der AcoustID-Postgres — verworfen
(engere Kopplung, schwerer zu debuggen). Eigener MB-Spiegel im Projekt —
bewusst ausgeschlossen.

## 2026-07-25: Wächter steuert den Stack über /var/run/docker.sock

Entscheidung: Nur der Wächter startet/stoppt die Stack-Container, direkt
über den gemounteten docker.sock. API und Importer steuern nie Docker.
Begründung: Einfachste zuverlässige Weck-Mechanik auf einem
Docker-Host/Unraid; Risiko bewusst akzeptiert und mitigiert durch
minimalen Code im Wächter, Passwort-Login, Rate-Limit, LAN-Betrieb.
Alternativen: Docker-API über TCP/Socket-Proxy oder externe
Automatisierung — im Handoff nicht vorgesehen; Risiko-Abwägung ist
dokumentierter Teil der Architektur-Session.

## 2026-07-25: Getrennte Images für Wächter, API und Importer

Entscheidung: Drei eigene Images (watchdog, api, importer) plus
offizielle Images für Postgres und acoustid-index; Release immer mit
einem gemeinsamen Tag für alle drei aus einem Actions-Workflow.
Begründung: Minimale Angriffsfläche im docker.sock-Container,
Update-Entkopplung (nur Wächter-Neustarts sind spürbar), kleiner
Dauerläufer auf dem Cache; Versionskonsistenz über den gemeinsamen Tag.
Alternativen: Ein gemeinsames Image für alles — verworfen
(Angriffsfläche, Größe des Dauerläufers, Update-Kopplung).

## 2026-07-25: Config, Keys und Logs beim Wächter auf dem Cache

Entscheidung: `config.yaml`, API-Keys, Admin-Login, Update-Historie und
Event-Log leben beim Wächter (SQLite + YAML auf dem Cache-Pool), nicht
in der Array-Postgres.
Begründung: Die Admin-UI muss bei schlafendem Stack voll funktionsfähig
sein; kein UI-Aufruf darf das Array wecken.
Alternativen: Zentrale Ablage in der Stack-Postgres — verworfen (würde
das Array bei jedem UI-Zugriff wecken).

## 2026-07-25: Technologie-Stack Python/FastAPI/Jinja2+HTMX/Postgres/SQLite

Entscheidung: Eine Sprache (Python) für Wächter, API und Importer;
FastAPI als Web-Framework; Admin-UI server-rendered mit Jinja2 + HTMX
ohne Frontend-Build; PostgreSQL für den Datenbestand, SQLite für den
Wächter-Zustand; immer neueste stabile Versionen zum
Implementierungszeitpunkt.
Begründung: Ein Sprach-Ökosystem für alles reduziert Pflegeaufwand;
server-rendered UI vermeidet Build-Pipeline und npm-Abhängigkeiten im
Wächter-Container.
Alternativen: Im Handoff nicht weiter dokumentiert (Ergebnis der
Architektur-Session).

## 2026-07-25: On-Demand-Betrieb — nur der Wächter weckt

Entscheidung: Der Stack schläft im Normalzustand; der Wächter weckt bei
eingehender API-Anfrage (Anfrage wird bis `wake.hold_timeout_s` gehalten)
und beim täglichen Update; Auto-Stopp nach `idle.timeout_min`, nur wenn
keine Anfragen liefen und kein Import-/Backup-Job aktiv ist.
Begründung: Erfolgskriterium: Array-Platten dürfen herunterfahren; ein
einziger kleiner Dauerläufer auf dem Cache.
Alternativen: Dauerbetrieb des Stacks — verworfen (widerspricht dem
Projektziel schlafender Platten).

## 2026-07-25: Stale-Serving statt Wartungsfenster beim Import

Entscheidung: Während des Delta-Imports werden Lookups aus dem alten
Bestand weiterbedient; jede Delta-Datei ist eine eigene Transaktion;
der Import ist resumierbar über `import_state`.
Begründung: Kein Wartungsfenster nötig; robust gegen Abbrüche auf
langsamen Spindeln.
Alternativen: Import mit Schreibsperre/Downtime — verworfen.

## 2026-07-25: Lookup-Cache im Wächter mit vollständiger Invalidierung

Entscheidung: Ergebnis-Cache (Hash aus Fingerprint+Duration+
meta-Parametern → Antwort) auf SSD im Wächter; Cache-Hits wecken das
Array nicht; nach jedem erfolgreichen Delta-Import und jeder lokalen
Submission wird vollständig invalidiert.
Begründung: Wiederholte Lookups (häufig bei Tagging-Läufen) sollen das
Array gar nicht erst wecken; vollständige Invalidierung ist einfach und
garantiert Kohärenz.
Alternativen: Selektive Invalidierung — verworfen (Komplexität ohne
belegten Nutzen).

## 2026-07-25: Defaults — auth.mode `none`, submit.mode `local`

Entscheidung: API-Auth default `none` (Betrieb im LAN/VPN), Submit
default `local`; bei Exponierung nach außen zwingend `apikey` +
Reverse-Proxy mit TLS (Doku-Pflicht).
Begründung: Reibungsloser LAN-Betrieb als Normalfall; Schutzschalter
vorhanden und per Admin-UI umschaltbar.
Alternativen: Default `apikey` — nicht gewählt. Der Default `none`
(Handoff §11.6, zunächst Annahme) wurde vom Auftraggeber am 2026-07-25
explizit bestätigt.

## 2026-07-25: Backup nur für lokale Unikate

Entscheidung: Der zeitgesteuerte Backup-Job sichert ausschließlich
`local_submission`-Daten und die Wächter-SQLite in `backup.dir`.
Begründung: Der öffentliche Datenbestand ist jederzeit aus den Dumps
rekonstruierbar; nur Eigenes ist unwiederbringlich.
Alternativen: Vollbackup der Postgres — verworfen (dreistellige
GB-Größe ohne Mehrwert).

## 2026-07-25: Bewusste Ausschlüsse (Scope)

Entscheidung: Kein eigener MB-Spiegel, kein serverseitiges
Fingerprint-Berechnen, keine Metadaten-Suche/kein Browsing, keine
Mehrbenutzer-Verwaltung, kein Kubernetes/Helm, keine Weiterverteilung
des Datenbestands.
Begründung: Reine Fingerprint-Auflösung als Kernauftrag; alles Weitere
ist anderweitig vorhanden oder Lizenz-/Betreiberthema.
Alternativen: Jeweils Aufnahme in den Scope — verworfen (Handoff §5,
„Bewusst ausgeschlossen").
