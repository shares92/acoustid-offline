# LEARNINGS.md — acoustid-offline

Erkenntnisse aus diesem Projekt, damit künftige Projekte besser laufen:
weniger Fehler, weniger Recherche, bessere Standards. Hier landet alles
Gelernte — Bugfixes (Ursache + Lösung), UI/UX-Korrekturen, technische
Standards, Anforderungs-Erkenntnisse, Prozess-Verbesserungen.

Format je Eintrag:
`## [Kategorie] Kurztitel` — Kategorien: Technik, Prozess, UI/UX, Bug,
Anforderung. Darunter: Was gelernt? Warum relevant? Wie künftig anwenden?

---

## [Technik] README/Doku sind keine Vertragsquelle — Code + Tests sind es

Was: Das acoustid-index-README beschreibt Response-Formate falsch
(z. B. `{}` statt `{"version": n}`), die offizielle API-Doku
verschweigt real existierende Parameter (`batch`, `maxdurationdiff`)
und zeigt falsche Typen (`index` als Zahl statt String). Die korrekten
Verträge standen in `src/api.zig`, den Tests und dem Server-Quellcode.
Warum: Wer gegen das README implementiert, baut inkompatibel — und
merkt es erst in der Integration.
Anwenden: API-Verträge immer aus Code + Tests der Referenz belegen;
Doku nur als Einstieg. Abweichungen Doku↔Code explizit dokumentieren
und im Zweifel code-treu implementieren.
Folgefund (Phase 5): Auch Code-Recherche ersetzt keinen Praxistest —
erst der Lauf gegen den echten Server zeigte u. a. Kurzfeldnamen auch
für Requests, stilles Deckeln von limit/timeout, `{"e": …}`-Fehlerrümpfe
und Indexnamen-Regeln. Vor der Integration einen empirischen
Abgleichslauf einplanen und die Befunde als Addendum an den
Recherche-Bericht hängen.

## [Technik] Auch offizieller Referenzcode kann defekt sein

Was: Die offizielle Python-Referenz von `extract_query` wirft für
~50 % realer Eingaben einen OverflowError (signed-Array + unsigned
Werte), und der Legacy-Suchpfad des offiziellen Servers liefert selbst
gehostet konstruktionsbedingt null Treffer (Index-Aufruf nur im
Nicht-Default-Zweig). Autoritativ ist die C-Extension.
Warum: „Aus dem offiziellen Repo kopiert" ist kein Korrektheitsbeweis;
tote/kaputte Pfade überleben dort, weil die Produktion sie nicht nutzt.
Anwenden: Nachbauten immer gegen die autoritative Implementierung
bit-genau verifizieren (hier: CI-Check gegen die C-Extension als
Test-Container); prüfen, welchen Codepfad die Produktion wirklich
nutzt, bevor man ihn als Referenz nimmt.

## [Technik] Bewegliche Image-Tags per Digest pinnen

Was: Das einzig brauchbare acoustid-index-Image existiert nur als
bewegliches `main`-Tag ohne Release-Prozess; `latest` zeigt auf einen
älteren Stand, `stable` auf eine inkompatible Alt-Implementierung.
Warum: Ein bewegliches Tag kann sich unbemerkt ändern (oder auf einen
inkompatiblen Rewrite springen); `latest` ≠ neuester Stand ist ein
häufiges Muster.
Anwenden: Fremd-Images ohne sauberen Release-Prozess immer per
`@sha256:`-Digest pinnen und die Tag-Semantik (latest/stable/main) je
Projekt einzeln prüfen.

## [Prozess] Host-abhängige Ressourcenwerte konfigurierbar machen

Was: Die Query-Hash-Anzahl (bestimmt Index-Größe ~40–55 vs. ~30–37 GB)
wurde auf Betreiber-Wunsch als Config-Wert statt Konstante ausgelegt:
„andere Systeme haben andere RAM-Größen".
Warum: Ein für die Referenz-Hardware optimierter Festwert macht das
Projekt auf kleineren Hosts unbrauchbar; die Messwerte eines Probelaufs
gehören in eine Empfehlungstabelle, nicht in Hardcode.
Anwenden: Bei verteilbarer Software jeden RAM-/Disk-relevanten
Stellhebel konfigurierbar machen, Default + Empfehlungstabelle
dokumentieren — und dazuschreiben, was eine Änderung kostet (hier:
Index-Neuaufbau).

## [Bug] httpx: iter_bytes(chunk_size) verliert beim Abbruch den Puffer

Was: `Response.iter_bytes(chunk_size)` puffert bis zur Blockgröße und
verwirft den Puffer bei Verbindungsabbruch — mit 1-MiB-Blöcken verliert
ein Download-Resume bis zu 1 MiB Fortschritt; im Extremfall wird gar
kein Range-Header gesendet, weil die .part-Datei leer bleibt. Richtig:
`iter_raw()` ohne Blockgröße (jeder Netzblock wird sofort geschrieben).
Warum: Der Fehler ist unsichtbar, solange Verbindungen halten, und
macht Resume-Logik wirkungslos, wenn sie am nötigsten ist.
Anwenden: Bei Streaming-Downloads mit Resume immer die rohe
Iterationsform verwenden und den Abbruch-Fall im Test simulieren
(Assertion auf die tatsächlich gesendeten Range-Header über mehrere
Versuche).

## [Technik] amd64-only-Images hängen unter qemu-Emulation still

Was: Das acoustid-index-Image (nur linux/amd64) startet auf Apple
Silicon unter colima-Default (qemu-binfmt) einen Prozess, der nie den
Listen-Socket öffnet und nichts loggt — kein Fehler, nur Hängen. Mit
`colima start --vz-rosetta` läuft es einwandfrei. Auf amd64-Hosts
(Unraid, CI) irrelevant.
Warum: „Container läuft" heißt unter Emulation nicht „Programm
funktioniert"; das Symptom (stilles Hängen) führt in stundenlanges
Fehlersuchen an der falschen Stelle.
Anwenden: Bei Fremd-Images zuerst die Architektur prüfen (`docker
manifest inspect`); auf ARM-Macs Rosetta-Modus aktivieren; in
Test-Doku festhalten, welche lokale VM-Konfiguration nötig ist.

## [Technik] Postgres-18-Image ändert das Volume-Layout

Was: Das offizielle postgres:18-Image deklariert `VOLUME
/var/lib/postgresql` mit `PGDATA=/var/lib/postgresql/18/docker` —
bisher üblich war der Mountpunkt `/var/lib/postgresql/data`. Wer den
alten Pfad mountet, hat ein Volume, das die Daten nicht enthält
(empirisch per `docker image inspect` belegt).
Warum: Der Fehler fällt erst beim Update/Restore auf, wenn die
vermeintlich persistierten Daten weg sind.
Anwenden: Bei Major-Upgrades offizieller Images VOLUME/ENV per
`docker image inspect` prüfen, nicht vom Vorgänger übernehmen;
Mountpunkt im Compose ist bei PG 18 das Elternverzeichnis.

## [Bug] YAML 1.1 liest unquotierte Uhrzeiten als Sexagesimal-Zahl

Was: PyYAML (YAML 1.1) parst `time: 14:30` ohne Anführungszeichen als
Integer **870** (Minuten-Interpretation der Sexagesimal-Notation) —
handgeschriebene Configs mit Uhrzeiten kippen still auf Zahlen. Eigene
Writes sind sicher (der Emitter quotet). Abgefangen per BeforeValidator
(Integer < 1440 → zurück nach HH:MM) + Test.
Warum: Der Fehler ist lautlos und produziert scheinbar valide, aber
falsche Werte — klassische Konfigurations-Zeitbombe.
Anwenden: Bei YAML-Configs jede „HH:MM"-, Versions- oder
Oktal-anfällige Eingabe (on/off/yes/no ebenso) mit explizitem
Validator härten und mit unquotierten Eingaben testen.

## [Bug] stdlib-logging: reservierte LogRecord-Feldnamen in extra

Was: `logger.info(msg, extra={"created": …})` wirft KeyError — aber
erst, wenn der Log-Level das Ereignis wirklich durchlässt; bei
gedrosseltem Level bleibt der Bug unsichtbar. Konvention jetzt:
Anwendungsfelder gebündelt unter einem eigenen `extra`-Objekt bzw. mit
sprechendem Präfix; Regressionstest vorhanden.
Warum: Level-abhängige Crashes überleben Tests, die mit anderem Level
laufen, und schlagen erst in Produktion zu.
Anwenden: Logging-Wrapper bauen, der Anwendungsfelder namespaced;
Log-Aufrufe in Tests mit durchlässigem Level ausführen.

## [Bug] CI-Action-Versionen gegen echte Tags prüfen — auch „verifizierte"

Was: Der Bau-Agent gab `astral-sh/setup-uv@v9` als „zum Stichtag
aktuell verifiziert" an; der erste CI-Lauf brach mit „unable to find
version v9" ab. Es existiert `v9.0.0` als exakter Tag, aber kein
beweglicher Major-Tag `v9` (anders als bei actions/checkout, wo `v7`
existiert). Fix: exakten Tag pinnen.
Warum: Ob ein Projekt bewegliche Major-Tags pflegt, ist je Repo
verschieden; „die Version existiert" heißt nicht „dieser Tag
existiert". Agenten-Verifikationsbehauptungen sind Prüf-Kandidaten,
keine Fakten.
Anwenden: Action-Referenzen vor dem Commit gegen
`gh api repos/<owner>/<repo>/tags` prüfen; nach jedem Push den ersten
CI-Lauf tatsächlich beobachten statt Grün anzunehmen (Orchestrator-
Verifikation hat den Fehler hier in Minuten gefangen).

## [Technik] Mehrere gleichnamige Python-Pakete kollidieren im venv

Was: Drei Services mit Verzeichnis `app/` (Handoff-Struktur) lassen
sich nicht als drei Pakete namens `app` in ein gemeinsames venv
installieren — sie überschreiben einander. Lösung: package-dir-Mapping
auf eindeutige Import-Namen (`acoustid_api` …) bei unveränderten
Verzeichnissen. Zusatzbefund: Hatchlings sources-Remapping mit
Präfix-Änderung wirft bei Editable-Installs einen harten ValueError —
setuptools kann es.
Warum: Monorepos mit mehreren Services tappen genau hier hinein, und
der Fehler zeigt sich erst beim zweiten installierten Paket.
Anwenden: Bei Multi-Service-Python-Repos Import-Namen von Anfang an
eindeutig wählen (Verzeichnisnamen dürfen Spezifikation bleiben);
Editable-Install-Verhalten des Build-Backends früh testen.
Folgefund (Phase 3): `import shared` schlägt fehl, wenn das CWD das
Repo-Root ist (Workspace-Member-Verzeichnis gewinnt als Namespace-Paket
gegen den Editable-Finder) — in Skripten/Docker-WORKDIR nie das
Repo-Root als Arbeitsverzeichnis verwenden.
Folgefund (Phase 4): Eine `conftest.py` im Repo-Root legt das
Wurzelverzeichnis auf `sys.path[0]` und reaktiviert die Kollision
(`--import-mode=importlib` hilft nicht); Lösung: im Root-conftest den
Wurzelpfad beim Laden wieder aus `sys.path` entfernen.

## [Prozess] Mechanisch hergeleitete Befunde empirisch gegenprüfen

Was: Die Code-Analyse leitete zwingend her, dass `meta-update`-Dateien
COPY-Text-escaped und damit teils ungültiges JSON seien — inklusive
Lade-Empfehlung (COPY-FROM-Staging). Ein 1-Minuten-Test gegen die echten
Fixtures widerlegte das: alles valides JSON; die empfohlene Staging-
Ladung hätte die Daten sogar korrumpiert.
Warum: „Aus dem Quellcode zwingend abgeleitet" ist keine Empirie; ein
plausibler Mechanismus kann an einer unbekannten Zwischenschicht
scheitern. Die Design-Folgen wären teuer gewesen.
Anwenden: Jede als „zwingend" markierte Herleitung, die Design-
Entscheidungen treibt, vor der Übernahme mit einem minimalen Experiment
gegen echte Daten testen — besonders wenn Recherche- und Verifikations-
Möglichkeit (Fixtures!) bereits nebeneinander vorliegen.
**Addendum (Phase 6):** Die Gegenprüfung selbst war korrekt — aber die
Stichprobe deckte nur EINE Epoche ab. Real war das Escaping bis
2024-12-04 vorhanden (~89 % der Historie), die „Widerlegung" galt nur
für neue Dateien. Bei langlebigen Datenquellen muss die Stichprobe
über die ZEITACHSE streuen (ältester Tag, Umbruchskandidaten,
neuester Tag), nicht nur über Kategorien; Formatumbrüche fallen gern
mit dokumentierten Betriebsstörungen zusammen (hier: Export-Ausfall
11/2024).

## [Technik] JSON-Dumps mit json_strip_nulls: fehlender Schlüssel = Wert

Was: AcoustID exportiert mit `json_strip_nulls` — ein fehlender
JSON-Schlüssel bedeutet NULL bzw. Default (false), niemals „Feld
unverändert lassen". Konkrete Falle: `track_mbid.disabled` erscheint nur
bei `true`; bei Reaktivierung kommt die Zeile ohne den Schlüssel, und
wer dann nicht explizit `false` setzt, behält ein falsches `true`.
Warum: Der intuitive „Patch"-Import (nur vorhandene Felder übernehmen)
erzeugt stille Datenfehler, die erst Monate später auffallen.
Anwenden: Bei jedem Fremd-Datenstrom zuerst die NULL-/Absent-Semantik
klären; Upserts immer mit vollständiger Spaltenliste inkl. expliziter
Defaults schreiben, nie als partieller Patch.

## [Technik] Suchindex: nur Query-Extrakte indexieren, nie Rohvektoren

Was: Fingerprint-Vollvektoren im acoustid-index bedeuten dokumentiert
~50 s pro Suche; mit extrahierten Query-Hashes (Offset 80, max. 120,
28-Bit-Maske, Silence-Filter) sind es ~50 ms. Zudem: Suchindizes dieser
Größenordnung (~40–50 GB) gehören auf SSD/in den RAM-Cache, nicht auf
HDD-Spindeln.
Warum: Der Faktor 1000 liegt nicht an Hardware, sondern an der
Datenmodellierung des Index — ein klassischer „falsches Ding indexiert"-
Fehler.
Anwenden: Vor jedem Index-Design prüfen, was die Referenz-Implementierung
tatsächlich indexiert (hier: `acoustid_extract_query` statt Rohdaten);
Index-Speicherort nach Zugriffsmuster wählen, nicht nach freiem Platz.

## [Prozess] Einheiten-Falle GB vs. GiB in Listings und Berichten

Was: Zwei unabhängige Recherchen lieferten scheinbar widersprüchliche
Volumenzahlen (414 „GB" vs. 385,6 „GB") — beide korrekt, nur einmal SI-
und einmal Binärpräfix; die HTML-Listings der Quelle labeln Binärwerte
als „MB/GB".
Warum: Solche Scheinwidersprüche kosten Klärungszeit oder führen zu
falschen Kapazitätsplanungen (~7 % Abweichung).
Anwenden: Bei Volumenangaben immer Bytes aus maschinenlesbaren Quellen
(hier `index.json`) ziehen und in Berichten beide Einheiten nennen;
bei Agenten-Synthese Einheiten normalisieren, bevor man Abweichungen
als Widerspruch behandelt.

## [Prozess] Risiko-zuerst-Phasenschnitt bei unverifizierten externen Formaten

Was: Das größte Projektrisiko (JSON-Dump-Format und Bootstrap-Weg sind
unverifiziert) wurde als Phase 0 vor jede Code-Zeile gezogen; das
DB-Schema bleibt bis dahin bewusst „logisch" statt exakt.
Warum: Ein früh festgezurrtes Schema auf Basis geratener Feldstrukturen
hätte Migrations- und Wegwerf-Arbeit in allen Folgephasen erzeugt.
Anwenden: Bei jedem Projekt mit fremden Datenformaten/APIs zuerst eine
reine Verifikationsphase einplanen und deren Ergebnis in die
Architektur-Doku zurückschreiben, bevor abhängige Phasen starten.

## [Prozess] Steuerungsdateien aus einem Handoff generieren

Was: Projektstart über ein Spezifikations-Handoff, aus dem
ARCHITECTURE.md (statische Referenz), PROGRESS.md (Phasen-Checkliste mit
DoD), DECISIONS.md (Entscheidungslog) und LEARNINGS.md erzeugt werden;
Implementierung strikt erst nach Go je Phase.
Warum: Trennt Spezifikation (unveränderlich, Quelle) von Arbeitsstand
(PROGRESS) und Begründungen (DECISIONS); Sessions bleiben auch nach
Kontextverlust arbeitsfähig (Session-Start = ARCHITECTURE + PROGRESS
lesen).
Anwenden: Gleiches Muster für künftige Projekte; Handoff nach docs/
kopieren, Steuerungsdateien in den Projekt-Root, Unklarheiten nie durch
Annahmen füllen, sondern als Klärungspunkte mit Optionen + Empfehlung
stellen.

## [Technik] psycopg3 schickt Python-Strings als *unknown* — Casts nur bei Arrays nötig

Was: Beim Phase-7-Import bleiben Zeitstempel und UUIDs als Strings,
Vektoren als `list[int]` — PostgreSQL castet sie aus dem Spaltenkontext
korrekt (auch mit Prepared Statements, empirisch verifiziert). Nur
Array-Vergleiche wie `id = ANY(%s)` brauchen einen expliziten Cast
(`%s::integer[]`), weil dort kein Spaltenkontext existiert.
Warum: Wer überall vorsorglich castet, verrauscht die Statements; wer
den Array-Fall vergisst, bekommt treiberabhängige Typfehler.
Anwenden: Parameter unkonvertiert lassen, Casts gezielt nur an Stellen
ohne Spaltenkontext setzen; bei Treiberwechsel den Array-Fall testen.

## [Technik] Batchgröße erst messen, dann tunen — hier zählt sie nur für den Speicher

Was: Beim Fingerprint-Import ist der Durchsatz (~9,2 MB gz/s lokal)
über Blockgrößen 250–4000 praktisch konstant; der RSS-Peak steigt aber
von 97 auf 164 MB. Engpass ist das Parsen/Schreiben je Zeile, nicht der
Overhead je `executemany`-Block.
Warum: Blindes Hochdrehen der Batchgröße hätte nur Speicher gekostet,
keinen Durchsatz gebracht.
Anwenden: Vor jedem Batch-Tuning eine kleine Messreihe (Durchsatz UND
Speicher je Blockgröße); die Stellschraube dokumentieren (`batch_rows`,
Default 1000) statt einen „optimierten" Magic Value einzubauen.

## [Technik] Sitzungsweite GUCs statt persistenter Schalter für Bulk-Phasen

Was: Der Bootstrap setzt `synchronous_commit=off` nur per `SET` in der
eigenen Sitzung und stellt beim Verlassen den Vorher-Wert wieder her
(nicht `RESET` — das ergäbe den Konfigurationswert, nicht den
Sitzungszustand beim Betreten). Voraussetzung: autocommit + keine
offene Transaktion, sonst rollt ein späterer Rollback das SET still weg.
Warum: Stirbt der Prozess mitten im Bulk, erlöschen
Sitzungseinstellungen von selbst — `ALTER SYSTEM` hätte eine
Produktions-DB dauerhaft unsicher zurückgelassen.
Anwenden: Unsichere Beschleuniger immer an die Lebensdauer der Sitzung
binden; alles, was ein Absturz nicht zurücknimmt, gar nicht erst
anfassen (fsync, full_page_writes).

## [Technik] Nachlauf-Arbeitsvorräte bestimmen die Index-Reihenfolge im Bulk-Import

Was: „Sekundärindizes erst nach dem Massenimport" kollidierte fast mit
dem Index-Feed: dessen Arbeitsvorrat ist der Partialindex
`fingerprint_idx_unindexed` — ohne ihn wäre jeder Feed-Batch ein
Seq-Scan über den Vollbestand. Lösung: core → Import → indexes → Feed.
Warum: Wer nur „Indizes zuletzt" denkt, übersieht Nachläufe, die selbst
auf einen Index angewiesen sind, und bezahlt mit stundenlangen Scans.
Anwenden: Vor dem Verschieben von Indizes ans Ende prüfen, welche
Folgeschritte welchen Index als Arbeitsvorrat brauchen, und die
Reihenfolge daran ausrichten.
