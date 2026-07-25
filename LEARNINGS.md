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
