# DECISIONS.md — acoustid-offline

Entscheidungslog. Neue Einträge oben anfügen. Format:
`## YYYY-MM-DD: Titel` / Entscheidung / Begründung / Alternativen.

---

## 2026-07-25: Korrektur — COPY-Escaping ist epochenabhängig; Parser-Regeln Phase 6

Entscheidung: Der Parser liest Delta-Dateien epochenabhängig: bis
einschließlich 2024-12-04 mit COPY-Text-Unescape (Backslashes
verdoppelt — betrifft praktisch nur `meta`), ab 2024-12-05 als reines
JSONL; je Zeile gibt es einen gezählten Fallback auf die andere
Lesart, Override per Parameter. Dies **korrigiert** den Eintrag
„Import-Verfahren" vom selben Tag: die dortige empirische Widerlegung
galt nur für die 2026er-Epoche (Stichprobe deckte die Zeitachse nicht
ab). Am Kernverfahren (zeilenweiser Parse + Batch-Upserts, kein
COPY-FROM-Staging) ändert sich nichts.
Weitere Phase-6-Entscheide: (a) **Lücken werden gemeldet, nicht
automatisch nachgeholt** — ein nachträglich eingespielter alter Tag
würde neuere Upsert-Stände überschreiben; (b) Zeitstempel/UUIDs
bleiben im Parser rohe Strings (Postgres castet; spart ein zweites
Parsen pro Zeile), Formprüfung per Regex; (c) Records als frozen
Dataclasses ohne Defaults (pydantic nur für Config — Heißpfad!); (d)
`verify_gzip` abschaltbar für den Bootstrap; (e) Downloader nutzt
`iter_raw()` statt `iter_bytes()` (Resume-korrekt).
Begründung: Empirisch belegt an Stichproben 2011–2026 und der neuen
Alt-Epochen-Fixture `2011-08-19-meta-update` (85/85 kaputte Zeilen
per Unescape wiederhergestellt; unabhängig von Agent und Fable
verifiziert). Ohne Epochen-Lesart wäre der Bootstrap an Tag 1,
Zeile 128 gescheitert — und scheinbar parsende Alt-Zeilen hätten
still falsche Werte geliefert.
Alternativen: Nur-JSONL-Lesart (bricht/verfälscht ~89 % der Historie),
automatisches Lücken-Backfill (Datenverfälschung), pydantic-Records
(Doppel-Validierung im Heißpfad) — verworfen.

## 2026-07-25: Index-Client — Bootstrap-Name, Healthcheck-Semantik, strikte Client-Validierung

Entscheidung: (a) Der Indexname kommt als Bootstrap-Variable
`AOFF_INDEX_NAME` (Default `main`) — der Container-Healthcheck braucht
ihn, und dort existiert keine config.yaml. (b) Der Compose-Healthcheck
prüft `/<name>/_health` (wget --spider; einziges HTTP-Tool im Image) —
der Dienst wird damit bewusst erst nach `ensure_index()` gesund:
Schreiber/Anleger (importer) hängen mit `condition: service_started`
ab, reine Leser (api) mit `service_healthy`. (c) Der Client validiert
`limit` (1–100), `timeout_ms` (1–10000), u32-Hashes und die
16-MiB-Grenze selbst, weil der Server Ausreißer teils still deckelt
statt abzulehnen.
Begründung: Empirische Befunde aus Phase 5 (siehe Addendum in
docs/research/phase1-acoustid-index.md); stilles Deckeln erzeugt
schwer diagnostizierbare Abweichungen.
Alternativen: Indexname in config.yaml (im Healthcheck nicht
verfügbar), Healthcheck nur auf `/_health` (prüft nichts), Serverwerte
ungeprüft durchreichen — verworfen.

## 2026-07-25: DB-Migrationen — eigener Runner, zwei Gruppen, lz4 in core

Entscheidung: Eigener schlanker Migrations-Runner in `shared/shared/db/`
(nummerierte SQL-Dateien als Package-Data, je Migration eine
Transaktion, `schema_migrations`-Protokoll mit Checksummen-Drift-
Erkennung, Advisory-Lock gegen parallele Starts, CLI
`python -m shared.db`) statt Alembic. Zwei Gruppen: `core` (Tabellen +
PKs) und `indexes` (Sekundärindizes) — der Bootstrap wendet erst
`core` an und zieht `indexes` nach dem Massenimport nach; global
aufsteigende Nummern über die Gruppen erzwingen identische Reihenfolge.
**`SET COMPRESSION lz4` liegt in `core`, nicht in `indexes`:** die
Einstellung wirkt nur auf neu geschriebene Werte — nach dem Bootstrap
gesetzt, bliebe genau der Erstbestand unkomprimiert (Fable-Entscheid
auf Agenten-Rückfrage).
Begründung: Raw-SQL-first-Design; Alembic brächte ORM-Kopplung ohne
Nutzen. Ein Test hält ARCHITECTURE-§5.2 und Migrations-SQL
anweisungsgleich — Doku und Schema können nicht divergieren.
Alternativen: Alembic (verworfen), lz4 in `indexes` (verworfen, s. o.),
FKs im Schema (bereits per §5.2 ausgeschlossen).
Nebenentscheide: Integrationstest-Schalter
`--integration=auto|require|off` (Abwahl immer sichtbar, `require`
scheitert laut); `tests/docker-compose.test.yml` publiziert 5432 nur
auf 127.0.0.1 für lokale Läufe — der Produktions-Compose bleibt bei
`expose`; `shared.db` bewusst nicht in `shared/__init__` re-exportiert
(psycopg lädt nur bei Bedarf; Wächter-Image bleibt schlank, optionales
Extra später möglich).

## 2026-07-25: Shared-Config — Designregeln (Phase 3)

Entscheidung (Paket zusammengehöriger Regeln):
- **Enum-Werte englisch** (`sleeping`, `forward_failed` …), da sie in
  YAML/JSON/SQLite/Postgres landen; die deutschen §9-Begriffe hängen
  als `display_name` an den Membern.
- **Leere Strings = „aus"** wird zentral über Properties abgebildet
  (`notify.enabled`, `backup.enabled`, `mb.configured`,
  `submit.upstream_enabled`); SMTP-Hauptschalter ist `host`
  (Port-Default 587, da ein Integer nicht „leer" sein kann); gesetzter
  Host verlangt from/to.
- **Fail fast:** `submit.mode: local+upstream` ohne
  `upstream_app_key` ist ein Validierungsfehler.
- **`mb.dsn` ohne Formatprüfung** (libpq akzeptiert URL- und
  Key-Value-Form); `notify.ntfy.url` muss http(s) sein.
- **Unbekannte Schlüssel:** Warnung mit vollem Pfad, dann ignorieren
  (upgrade-/downgrade-freundlich).
- **Secrets** als SecretStr, maskiert in repr/Dict; nur `save_config`
  schreibt Klartext — Datei mit Modus 0600.
- **`AOFF_LOG_LEVEL`** als zusätzliche Bootstrap-Env-Variable (das Log
  steht vor dem config.yaml-Read); kein pydantic-settings (11
  Variablen, eigene testbare from_env-Klasse).
Begründung: Konsistente, testbare Semantik an einer Stelle statt
verstreuter Konventionen; §6/§7-Anforderungen (Secrets nie im
Klartext, Modi-Schalter) direkt im Schema durchgesetzt.
Alternativen: deutsche Enum-Werte (bricht API-/DB-Konsistenz),
strikte Ablehnung unbekannter Schlüssel (bricht Upgrades),
pydantic-settings (unnötige Abhängigkeit) — verworfen.
Hinweis: config.yaml-Kommentare überleben ein Schreiben durch die
Admin-UI nicht (safe_dump); falls später nötig → ruamel.yaml.

## 2026-07-25: Python-Paketierung — Verzeichnisse nach §10, eigene Import-Namen

Entscheidung: Die Verzeichnisse bleiben exakt wie ARCHITECTURE §10
(`api/app`, `importer/app`, `watchdog/app`, `shared/`), installiert
werden die Pakete aber als `acoustid_api`, `acoustid_importer`,
`acoustid_watchdog` und `shared` (uv-Workspace, setuptools-Backend,
package-dir-Mapping; `shared/` mit einer Verschachtelungsebene
`shared/shared/`).
Begründung: Drei Pakete namens `app` würden sich in einem gemeinsamen
venv gegenseitig überschreiben; Workspace-Member-Wurzel und
Paketverzeichnis können nicht dieselbe Ebene sein. Hatchling scheidet
aus: Präfix-änderndes sources-Remapping bricht bei Editable-Installs
(empirisch verifiziert).
Alternativen: Verzeichnisse umbenennen (Abweichung von §10 sichtbar
statt intern), Hatchling (Editable-Bruch) — verworfen.
Folge: Neue Unterpakete müssen in `packages = [...]` der jeweiligen
pyproject.toml eingetragen werden (kein Auto-Discovery); Docker-Images
starten später z. B. `acoustid_api.main:app`.

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
sogar korrumpieren. **[Korrigiert am selben Tag, Phase 6: gilt nur für
die Epoche ab 2024-12-05 — siehe Eintrag „COPY-Escaping ist
epochenabhängig".]** Bulk-Muster (Indizes nachziehen, Batches, Prefetch,
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

## 2026-07-25: Phase-7-Import-Details (Upsert-, Zeit- und Feed-Semantik)

Entscheidung: (1) `imported_at` wird auch bei Konflikt auf `now()`
gesetzt — es protokolliert wie `src_day` die *letzte* Anwendung.
(2) `src_day`/`imported_at` schreiben beide Fingerprint-Ströme; die
Disjunktheitsregel gilt nur für Dump-Spalten. (3) `created` bei
`fingerprint` per `COALESCE(bestehend, neu)` — keiner der beiden Ströme
überschreibt es. (4) `track_fingerprint.fingerprint_id != id` bricht
die Datei-Transaktion hart ab (Zusicherung aus §5.1; ein Bruch hieße:
Zuordnung an falscher Zeile). (5) `import_state.started_at` = `now()`,
`finished_at` = `clock_timestamp()`, sonst wäre die Importdauer
konstant null. (6) Index-Feed: erst `_update`, dann `indexed_at`;
Vektoren ohne indexierbare Hashes gelten als erledigt (sonst ewig im
Arbeitsvorrat), Zeilen ohne Vektor bleiben offen. (7) Feed-Batches per
Vorgabe mit `expected_version` abgesichert; ein zweiter Schreiber führt
zu lautem Abbruch. (8) `feed_index` ruft per Vorgabe `ensure_index()`
(der Importer legt den Index an, Compose-Healthcheck wird danach grün).
(9) Testinfrastruktur: conftest-Marker `db` zusätzlich zu `index`,
damit ein Test beide Dienste anfordern kann (rückwärtskompatibel).
Begründung: Konsistente Buchführungs-Semantik, laute statt stiller
Fehler bei Zusicherungsbrüchen, kein stiller Datenverlust im Feed.
Alternativen: Teil-Patches nur vorhandener Felder (verworfen —
Reaktivierungs-Falle §5.1); `indexed_at` vor dem `_update` (verworfen —
stiller Datenverlust); Erst-Import-Zeit in `imported_at` behalten
(verworfen — widerspräche `src_day`-Semantik „letzte Anwendung").

## 2026-07-25: Phase-8-Job-Details (Bulk-Sicherheit, Guard, Report)

Entscheidung: (1) Bulk-Modus = ausschließlich `synchronous_commit=off`,
als Sitzungseinstellung mit Rücknahme auf den *Vorher*-Wert (nicht
`RESET`); `fsync`/`full_page_writes` und jedes `ALTER SYSTEM`/`ALTER
DATABASE` sind tabu. Zusätzlich `maintenance_work_mem=1GB` nur für den
Indexbau. (2) `update.min_free_gb` wird als GiB gelesen (strengere
Lesart), `0` schaltet den Guard ab; gemessen wird das
Dump-Verzeichnis. (3) Der Index-Feed läuft im Bootstrap erst nach der
Gruppe `indexes`. (4) Eingespielte Tagesdateien werden gelöscht
(`--keep-dumps` behält sie) — 414 GB aufzuheben wäre teuer und
nutzlos. (5) `--end-date` benennt den letzten einzuschließenden Tag.
(6) Report per Default als JSON auf stdout, Datei atomar
(`.part`+Rename); 9 Exit-Codes bijektiv zu Ergebnissen (Test hält das
fest). (7) Zwei Compose-Variablen bewusst ohne `AOFF_`-Präfix
(`ACOUSTID_IMPORTER_IMAGE`, `ACOUSTID_WATCHDOG_DATA`), damit der
`AOFF_`-Satz deckungsgleich mit shared/env.py bleibt (Test vorhanden).
(8) importer/Dockerfile schon jetzt (Phase 29 übernimmt ihn für den
Release-Build); config.yaml wird read-only unter `/watchdog` gemountet.
Begründung: Die Sitzung ist das Sicherheitsnetz, das ein Prozesstod
nicht aushebeln kann; Korruptionsrisiken (fsync) sind mit Resume nicht
reparierbar und bleiben draußen; der Rest folgt „laut scheitern,
maschinenlesbar berichten".
Alternativen: `fsync=off` für mehr Durchsatz — verworfen (korruptes
Cluster statt verlorener Schwanz-Transaktionen); persistente
PG-Schalter — verworfen; Dumps behalten als Default — verworfen.

## 2026-07-25: Phase-9-Lookup-Details (Pipeline- und Formatentscheide)

Entscheidung: (1) Ergebnisliste wird auf 10 gekappt, DANACH je Track
dedupliziert — Original-Verhalten, auch wenn dadurch weniger als 10
Treffer übrig bleiben können. (2) Die Track-Auflösung folgt der
Merge-Verkettung über `track.new_id` (Tiefe ≤ 10), anders als das
Original — unser Bestand kommt aus den Deltas, dort bleibt
`fingerprint.track_id` am zurückgezogenen Track stehen. (3) Antwortet
der acoustid-index nicht, gibt es Fehler 13/HTTP 503 statt der stillen
leeren Trefferliste des Originals (kein erfundenes „kein Treffer").
(4) `compare2` und der Chromaprint-Codec liegen als pure
stdlib-Algorithmen in `shared/shared/fingerprint/`; `extract_query`
bleibt bei `shared.fpindex` (es definiert den Indexinhalt).
(5) gzip-Sonderfälle: kaputter gzip-Rumpf gilt als leerer Rumpf +
WARNING (die 19er-Tabelle kennt keinen Code dafür; das Original wirft
einen nackten 400); zu großes Content-Length bei gzip ⇒ 19/413.
`multipart/form-data` wird nicht gelesen (kein Lookup-Client nutzt es).
(6) Kandidatenlimit 40 (ARCHITECTURE erlaubt 20–40), `client` bleibt
Pflichtparameter wie im Original (nur Anwesenheit geprüft — Auth macht
der Wächter). (7) Testschalter `ACOUSTID_EXTENSION_DSN` ohne
`AOFF_`-Präfix (wie `ACOUSTID_INTEGRATION_TESTS`).
Begründung: Kompatibilität dort, wo Clients sie messen können
(Format, Reihenfolge, Limits); laute Fehler dort, wo das Original
Information verschluckt; Delta-Realität schlägt Original-Codepfad bei
der Merge-Verkettung.
Alternativen: Deduplizieren vor dem Kappen (verworfen — messbar anderes
Antwortverhalten als das Original); leere Liste bei Index-Ausfall
(verworfen — maskiert Betriebsfehler); compare2 im api-Paket
(verworfen — Domänenalgorithmus, nicht API-spezifisch).

## 2026-07-25: Phase-10-MB-Details (Query-Schicht, meta, Degradation)

Entscheidung: (1) Die MB-Schicht liegt in `shared/shared/mb/` (nicht
im api-Paket), weil der Wächter in Phase 25 den MB-Verbindungstest
braucht (`MbClient.check_connection()` liegt dafür bereit); Treiber ist
psycopg3 + psycopg_pool — die SQLAlchemy-Formulierungen des
Phase-1-Berichts beschreiben die Referenz, kein SQLAlchemy/mbdata im
Projekt. (2) Neuer Config-Schlüssel `mb.keep_submitted_mbid` (bool,
Default `false`): standardmäßig trägt die Antwort die **kanonische**
MBID aus der Redirect-Auflösung; `true` reicht die eingereichte durch.
(3) Fehlerbild ⇒ HTTP: `MbUnavailable` UND `MbSchemaMismatch`
degradieren zu 200 ohne Metadaten (§8.7); `MbQueryError` ⇒ 5/500.
SQLSTATE-Zuordnung: fehlende Tabelle/Spalte/Rechte/Schema ⇒ Mismatch
(Dauerzustand → degradieren), `statement_timeout` ⇒ Unavailable, Rest
⇒ QueryError. (4) Der Circuit-Breaker zählt Erreichbarkeit, nicht
Korrektheit (`MbQueryError` zählt nicht; Mismatch zählt); bei bekanntem
Selfcheck-Mismatch wird gar nicht erst abgefragt. (5) meta-Präzedenz
ist die Wahl des **Wurzelzweigs** (if/elif-Kette wie
`inject_metadata` im Original-Quelltext, an diesem belegt) — die
übrigen Schlüsselwörter wirken als Detail-Modifikatoren im gewählten
Zweig. (6) Metadaten werden einmal je Anfrage über eine gemeinsame
`track_id`-Zuordnung injiziert (Original-Verhalten); `recordingids`
nutzt die Index-Only-Existenzprüfung statt der Vollabfrage.
(7) Betriebswerte als dokumentierte Konstanten statt Config: Breaker
3 Fehler/30 s/30 s, Zeilenlimit 5000 + Truncation-Flag, connect 2 s,
statement 2000 ms, Pool max 4, Staleness 36 h/168 h, erwartete
Schema-Sequenz 31. (8) `sources` (track_mbid.submission_count) und
`usermeta` (meta/track_meta) kommen vollständig aus dem Delta-Bestand.
Begründung: ein Treiber im Projekt; §8.7 verlangt Degradation nur für
Nichterreichbarkeit — ein dauerhaft passendes Schema ist dem
gleichgestellt, echte Abfragefehler dürfen nicht leise verschwinden;
Kompatibilität dort, wo Clients sie messen (Präzedenz, compress-/
m2-Eigenheiten bug-für-bug, tabelliert in docs/api-lookup.md).
Alternativen: SQLAlchemy-Schicht (verworfen — neue Abhängigkeit ohne
Mehrwert); Schema-Mismatch als 500 (verworfen — degradierter Betrieb
ist das dokumentierte Verhalten bei kaputtem Spiegel); Config-Schlüssel
für Breaker/Timeouts (verworfen — ohne Messwerte vom echten Spiegel
wären es Scheinstellschrauben).
