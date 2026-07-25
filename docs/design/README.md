# Handoff: acoustid-offline Admin-UI

## Overview

Admin-Oberfläche für das Selfhosting-Projekt **acoustid-offline**. Sie läuft im
Wächter-Container unter `/admin`, hat einen Admin-Benutzer mit Passwort-Login und
Session-Cookie, und dient dazu, den AcoustID-Stack zu beobachten und zu steuern,
**ohne das Storage-Array unnötig zu wecken**.

Die zentrale Produktidee prägt das gesamte Design: Das Array soll möglichst
schlafen. Deshalb ist der Zustand `schlafend` der **erwünschte Normalzustand** und
wird positiv-neutral dargestellt — ruhig, ohne Warnfarbe, ohne Animation. Jede
Aktion, die das Array weckt, ist explizit als solche gekennzeichnet und braucht
eine Bestätigung.

Sieben Views: Login, Dashboard, Konfiguration, API-Keys, Updates & Jobs,
Ereignis-Log, Statistiken.

## About the Design Files

Die Datei in `prototype/` ist eine **Design-Referenz, umgesetzt in HTML** — ein
Prototyp, der Aussehen und Verhalten zeigt. Sie ist **kein Produktionscode zum
Kopieren**. Der Prototyp verwendet React-artige Client-State-Logik, nur damit alle
Zustände im Browser durchgeschaltet werden können.

Die Zielumgebung ist festgelegt und nicht verhandelbar:

- **FastAPI + Jinja2-Templates + HTMX**
- **Kein Frontend-Build, kein npm, kein SPA-Framework**
- CSS: **eine handgeschriebene Stylesheet-Datei**
- Dynamik über HTMX: Partial-Updates und Polling, keine WebSockets
- UI-Sprache **Deutsch**; technische Begriffe (Lookup, Submit, Delta, Cache-Hit)
  bleiben englisch

**Die Aufgabe ist also: dieses Design in Jinja2-Templates + einem handgeschriebenen
Stylesheet + HTMX nachbauen.** Der ganze Client-State des Prototyps wird
serverseitig aufgelöst — welche Buttons sichtbar sind, welche Badges gerendert
werden, welche Zeilen die Tabelle hat. Übernimm aus dem Prototyp: Layout,
Abstände, Farben, Typografie, Copy, Interaktionsregeln.

Zum Ansehen: `prototype/acoustid-offline-admin.dc.html` im Browser öffnen —
`support.js` muss im selben Ordner liegen, sonst bleibt die Seite leer. Unten
rechts liegt eine **Prototyp-Steuerung**, mit der sich alle Zustände durchschalten
lassen (Stack-Zustände, Neben-Badges, Leerzustände, Login-Varianten). Diese Leiste
ist reines Abnahme-Werkzeug und wird **nicht** implementiert.

`DESIGN_HANDOFF_original.md` ist die Ausgangsspezifikation und bleibt die Quelle
für alles Fachliche.

## Fidelity

**High-fidelity.** Farben, Typografie, Abstände, Zustände und Copy sind final.
Alle Farbwerte sind als CSS-Custom-Properties dokumentiert und sollen 1:1
übernommen werden. Die exakte Pixelgeometrie der Screenshots ist zweitrangig
gegenüber den dokumentierten Werten — bei Abweichung gilt die Wertetabelle.

---

## Design Tokens

Ein Token-Set, zwei Modi. Im Stylesheet als zwei Blöcke; der Modus hängt an einem
Attribut auf `<body>`:

```css
:root, body[data-ao-theme="dark"] { color-scheme: dark;  /* dark values */ }
body[data-ao-theme="light"]       { color-scheme: light; /* light values */ }
```

Dark ist der Fallback ohne Attribut. Der Server rendert das Attribut aus einer
User-Präferenz (`dark` / `light` / `system`); bei `system` entscheidet ein
6-Zeilen-Inline-Script anhand `prefers-color-scheme`.

| Token | Rolle | Dark | Light |
|---|---|---|---|
| `--bg` | Seitenhintergrund, Input-Flächen | `oklch(0.18 0.008 250)` | `oklch(0.965 0.002 250)` |
| `--surface` | Karten, Sidebar, Dialoge | `oklch(0.225 0.009 250)` | `oklch(1 0 0)` |
| `--surface-2` | Hover, Badge-Flächen, Buttons | `oklch(0.27 0.010 250)` | `oklch(0.945 0.003 250)` |
| `--line` | Trennlinien, Tabellenzeilen | `oklch(0.315 0.012 250)` | `oklch(0.900 0.004 250)` |
| `--line-2` | Rahmen Selects/Chips | `oklch(0.375 0.013 250)` | `oklch(0.850 0.005 250)` |
| `--line-3` | Rahmen Inputs/Buttons | `oklch(0.43 0.014 250)` | `oklch(0.775 0.007 250)` |
| `--txt` | Primärtext | `oklch(0.94 0.005 250)` | `oklch(0.215 0.008 250)` |
| `--txt-1` | kräftiger Sekundärtext | `oklch(0.80 0.010 250)` | `oklch(0.340 0.010 250)` |
| `--txt-2` | Fließtext sekundär | `oklch(0.69 0.012 250)` | `oklch(0.460 0.012 250)` |
| `--txt-3` | Labels, Feldbeschriftungen | `oklch(0.60 0.012 250)` | `oklch(0.545 0.011 250)` |
| `--txt-4` | Hilfstexte, Metadaten | `oklch(0.52 0.012 250)` | `oklch(0.630 0.010 250)` |
| `--on-acc` | Text auf Akzentfläche | `oklch(0.17 0.02 250)` | `oklch(0.99 0.002 250)` |
| `--acc-tint` | Info-Flächen | `oklch(0.24 0.022 240)` | `oklch(0.960 0.020 250)` |
| `--acc-line` | Info-Rahmen | `oklch(0.40 0.055 240)` | `oklch(0.830 0.055 250)` |
| `--acc-line-2` | Rahmen Primary-Button | `oklch(0.62 0.105 240)` | `oklch(0.640 0.130 250)` |
| `--acc-fg` | Akzenttext, Statuspunkte | `oklch(0.72 0.11 240)` | `oklch(0.510 0.150 250)` |
| `--acc-solid` | Primary-Button, Fortschritt | `oklch(0.65 0.115 240)` | `oklch(0.545 0.155 250)` |
| `--acc-solid-h` | Primary-Button Hover | `oklch(0.72 0.12 240)` | `oklch(0.480 0.160 250)` |
| `--link` / `--link-h` | Links | `oklch(0.74 0.10 240)` / `oklch(0.83 0.11 240)` | `oklch(0.505 0.150 250)` / `oklch(0.400 0.145 250)` |
| `--ok-tint` / `--ok-tint-h` | Erfolg-Fläche | `oklch(0.25 0.032 155)` / `oklch(0.30 0.045 155)` | `oklch(0.955 0.030 155)` / `oklch(0.925 0.045 155)` |
| `--ok-line` | Erfolg-Rahmen | `oklch(0.44 0.075 155)` | `oklch(0.810 0.070 155)` |
| `--ok-fg` | Erfolg-Text/Punkt | `oklch(0.75 0.13 155)` | `oklch(0.480 0.130 155)` |
| `--warn-tint` | Warnung-Fläche | `oklch(0.25 0.029 82)` | `oklch(0.960 0.045 90)` |
| `--warn-line` | Warnung-Rahmen | `oklch(0.41 0.055 82)` | `oklch(0.815 0.075 85)` |
| `--warn-fg` | Warnung-Text/Punkt | `oklch(0.80 0.12 82)` | `oklch(0.500 0.115 70)` |
| `--warn-fg-2` | Warnung-Fließtext | `oklch(0.80 0.030 82)` | `oklch(0.430 0.045 70)` |
| `--err-tint` / `--err-tint-h` | Fehler-Fläche | `oklch(0.235 0.032 27)` / `oklch(0.30 0.050 27)` | `oklch(0.960 0.028 27)` / `oklch(0.925 0.045 27)` |
| `--err-line` | Fehler-Rahmen | `oklch(0.44 0.080 27)` | `oklch(0.815 0.075 27)` |
| `--err-fg` | Fehler-Text/Punkt | `oklch(0.74 0.145 27)` | `oklch(0.495 0.175 27)` |
| `--err-fg-2` | Fehler-Fließtext | `oklch(0.80 0.030 27)` | `oklch(0.440 0.055 27)` |
| `--err-solid` / `--on-err` | destruktiver Button | `oklch(0.46 0.14 27)` / `oklch(0.97 0.01 27)` | `oklch(0.520 0.190 27)` / `oklch(0.99 0.005 27)` |
| `--scrim` | Dialog-Overlay | `oklch(0.10 0.005 250 / 0.66)` | `oklch(0.45 0.012 250 / 0.42)` |
| `--shadow` | Dialog-Schatten | `oklch(0.08 0.01 250 / 0.55)` | `oklch(0.50 0.02 250 / 0.16)` |
| `--header-bg` | Sticky-Header (blur) | `oklch(0.195 0.008 250 / 0.94)` | `oklch(0.975 0.002 250 / 0.92)` |
| `--focus` | Focus-Ring | `oklch(0.40 0.06 240 / 0.28)` | `oklch(0.62 0.13 250 / 0.24)` |

Wichtig zur Light-Variante: Es ist **keine Helligkeitsspiegelung**. Im Dark-Modus
ist die Karte heller als die Seite, im Light-Modus ist sie weiß auf hellgrauer
Seite — die Ebenenlogik bleibt, die Werte sind neu gesetzt. Semantische Farben
bekommen auf Weiß mehr Chroma und weniger Lightness.

### Typografie

Keine Webfonts — der Container hat oft keinen Internet-Zugang, ein
Google-Fonts-Link wäre ein Single Point of Failure.

```css
--font-sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI",
             "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
```

`body`: `font-size: 13px; line-height: 1.5; font-variant-numeric: tabular-nums`.
Tabellarische Ziffern gelten global — Zahlen in Tabellen und Statuszeilen dürfen
beim Polling nicht springen.

| Rolle | Größe / Weight / Sonstiges |
|---|---|
| Seitentitel (`h1` im Header) | 15px / 600 / lh 1.25 |
| Dialog-Titel | 14.5px / 600 / lh 1.35 |
| Karten-Titel, Gruppentitel | 13px / 600 |
| Karten-Überschrift (`STACK-ZUSTAND`) | 11px / 600 / `letter-spacing: 0.09em` / uppercase / `--txt-4` |
| Feld-Label | 11px / 600 / `letter-spacing: 0.07em` / uppercase / `--txt-3` |
| Tabellen-Header | 10.5px / 600 / `letter-spacing: 0.08em` / uppercase / `--txt-4` |
| Fließtext | 12.5px / 400 |
| Hilfstext unter Feldern | 11.5px / 400 / `--txt-4` |
| Große Kennzahl | 20–21px / 600 / mono |
| Mittlere Kennzahl (Delta-Sequenz) | 17px / 600 / mono |
| Zeitstempel, Keys, Pfade, Dauern | 11.5–12px / 400 / mono |
| Badge-Label | 11–11.5px / 600 |
| Badge groß (Stack-Zustand) | 14px / 600 |

Alle mehrzeiligen Fließtexte: `text-wrap: pretty`.

### Geometrie

| Wert | Verwendung |
|---|---|
| `2px` | Badge-/Chip-Radius |
| `3px` | Buttons, Inputs, Selects, Tags, innere Boxen |
| `4px` | Karten, Panels |
| `5px` | Dialoge, Login-Karte |
| `1px solid` | alle Rahmen |
| `3px solid` links | Akzentkante Stack-Status-Karte (Farbe = Zustandsfarbe) |
| `2px solid` links | aktives Sidebar-Item (`--acc-fg`) |

Abstände (durchgängig `gap`, nie Margins zwischen Geschwistern):
`4 · 5 · 6 · 7 · 8 · 9 · 10 · 11 · 12 · 13 · 14 · 16 · 18 · 20 · 22px`.
Karten-Innenabstand `14–18px`, Karten-Kopf `11–12px 16–18px`,
Content-Padding `20px 22px 96px`, Karten-Grid-Gap `14px`.

Höhen: Buttons `32–34px` (Login/Dialog `36–38px`), Inputs/Selects `34px`
(Filter-Selects `28–30px`), Badges `21–34px`.
Fortschrittsbalken `7px`, kleine Meter `4–5px`.

Layout-Maße: Sidebar `216px` fest. Content `max-width: 1320px`
(Konfiguration `820px`, Keys `1000px`, Jobs `1060px`, Logs/Stats `1120px`).
Karten-Grids: `repeat(auto-fit, minmax(288px, 1fr))` auf dem Dashboard,
`minmax(240px, 1fr)` in Konfigurationsgruppen und Job-Aktionen,
`minmax(178px, 1fr)` für KPI-Kacheln, `minmax(340px, 1fr)` für Charts.

### Schatten und Effekte

- Dialog: `0 18px 48px var(--shadow)`
- Toast: `0 8px 24px var(--shadow)`
- Sticky-Header: `background: var(--header-bg); backdrop-filter: blur(8px)`
- Dialog-Overlay: `background: var(--scrim); backdrop-filter: blur(2px)`
- Focus: `border-color: var(--acc-line-2); box-shadow: 0 0 0 3px var(--focus)`

### Keyframes

```css
@keyframes ao-pulse  { 0%,100%{opacity:1;transform:scale(1)}
                       50%{opacity:.35;transform:scale(.78)} }   /* 1.5s ease-in-out infinite */
@keyframes ao-stripe { from{background-position:0 0} to{background-position:22px 0} } /* 0.9s linear infinite */
@keyframes ao-fade   { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} } /* 0.14–0.16s ease-out */
```

---

## Das Zustandsmodell

Das wichtigste UI-Element. Auf **jeder** Seite oben rechts im Header als kompakter
Badge, auf dem Dashboard zusätzlich als große Karte.

| Zustand | Label | Farbe | Punkt | Headline | Sub |
|---|---|---|---|---|---|
| `schlafend` | „schlafend“ | `--txt-1` auf `--surface-2`, Rahmen `--line-3` | statisch | „Array darf schlafen“ | „Schläft seit 6 Std. 12 Min. · Letzter Weckvorgang heute 03:00 · 4 Lookups aus dem Cache beantwortet“ |
| `startet` | „startet“ | `--warn-fg` / `--warn-tint` / `--warn-line` | `ao-pulse` | „Wächter fährt den Stack hoch“ | „Array geweckt · Container acoustid-index, acoustid-api, postgres starten · seit 00:12“ |
| `bereit` | „bereit“ | `--ok-fg` / `--ok-tint` / `--ok-line` | statisch | „Stack läuft — Lookups gehen direkt“ | „Uptime 00:41:08 · Idle-Timeout in 17 min · 3 Container healthy“ |
| `stoppt` | „stoppt“ | `--warn-fg` / `--warn-tint` / `--warn-line` | `ao-pulse` | „Idle-Timeout erreicht, Stack fährt herunter“ | „Keine Lookups seit 20 min · Container werden gestoppt, danach darf das Array schlafen“ |
| `fehler` | „Fehler“ | `--err-fg` / `--err-tint` / `--err-line` | `ao-pulse` | „Stack konnte nicht gestartet werden“ | „Letzter Versuch heute 03:00 · Automatische Wiederholung ausgesetzt“ |

**`schlafend` ist der einzige Zustand mit fast farbloser Darstellung und ohne
Animation.** Das ist Absicht und darf nicht „verbessert“ werden: Ruhe muss
visuell Ruhe sein. Nur Übergänge und Fehler pulsieren.

Badge-Bauform (beide Größen): rechteckiges Tag, `border-radius: 3px`, 1px Rahmen
in der Zustands-Rahmenfarbe, getönte Fläche, links ein runder Punkt
(kompakt `7px`, groß `9px`) in der Zustandsfarbe, dann das Label.
**Keine Pillen** — Pillen wirken konsumentig.

### Parallel anzeigbare Neben-Badges

Eigene Zeile unter dem Header, nur gerendert wenn mindestens eines aktiv ist.
Gleiche Bauform, Höhe `24px`, Punkt `5px`, `flex-wrap`, auf Mobil horizontal
scrollbar.

| Badge | Farbe | Punkt | Bedingung |
|---|---|---|---|
| `Import läuft — Datei {x} von {y}` | Akzent | `ao-pulse` | Import aktiv |
| `Backup läuft` | Akzent | `ao-pulse` | Backup aktiv |
| `Upstream-Queue: {N} wartend` | neutral (`--txt-1`) | statisch | nur wenn `N > 0` |
| `MB nicht erreichbar — degradierter Betrieb` | Warnung | statisch | MusicBrainz-DSN down |
| `Plattenplatz knapp — {frei} frei` | Warnung | statisch | unter Warnschwelle |

---

## Screens

Persistente **Sidebar** links, `216px`, `--surface`, 1px Rahmen rechts.
Kopf: Produktname (mono, 13.5px/600) + „Wächter-Admin · 0.9.4“ (mono, 11px,
`--txt-4`). Dann die sechs Bereiche. Fuß: Theme-Segment (Hell / Auto / Dunkel)
und „Abmelden“.

Nav-Item: `min-height 34px`, `padding 0 15px`, 2px transparenter linker Rahmen;
aktiv → `background: --surface-2`, `color: --txt`, linker Rahmen `--acc-fg`;
Hover → `background: --surface-2`. Rechts optional ein Zähler-Chip
(mono, 10.5px/600, `--surface-2`): API-Keys zeigt die Key-Anzahl,
Updates & Jobs eine `1` bei laufendem Job.

Begründung Sidebar statt Topbar: sechs Bereiche plus Logout brauchen vertikalen
Platz; die Topbar bleibt frei für Seitentitel, Route, Polling-Hinweis,
Stack-Badge und die Neben-Badges-Zeile.

Header (sticky, `z-index 20`): links `h1` + Route in mono (`--txt-4`), rechts
Polling-Hinweis + Stack-Badge.

### Login — `/admin/login` · `24-login-erststart.png`, `25-login-fehlversuch.png`

Zentrierte Karte, `max-width 392px`, Seitenhintergrund
`radial-gradient(120% 80% at 50% 0%, var(--surface-2) 0%, var(--bg) 70%)`.

Über der Karte Produktname + Version. In der Karte: „Admin-Anmeldung“, darunter
„Benutzer **admin** · Session-Cookie, 12 Std. gültig“. Ein Passwortfeld
(`38px`, Label uppercase), Submit „Anmelden“ (Primary, volle Breite).
Unter der Karte: „Läuft im Wächter-Container. Die Anmeldung weckt das Array nicht.“

Drei Zusatzzustände:

- **Erststart:** Info-Box (Akzent-Tint) über dem Feld, Titel „ERSTER START“,
  Text „Das Erst-Passwort steht im Container-Log des Wächters.“, dann ein
  `<code>`-Block `docker logs acoustid-waechter | grep Erst-Passwort`
  (mono 11.5px, `--bg`, 1px Rahmen, horizontal scrollbar).
- **Fehlversuch:** Feldrahmen `--err-fg`, darunter linksbündige Fehlerzeile mit
  2px linkem Rahmen in `--err-fg` auf `--err-tint`:
  „Passwort falsch. Versuch {n} von 5.“
- **Rate-Limit:** Warn-Box, Titel „Zu viele Fehlversuche“, Text „Anmeldung für
  diese IP gesperrt. Nächster Versuch in **04:32** min.“, Submit gesperrt.

### Dashboard — `/admin/` · `01`–`04`

Karten in Prioritätsreihenfolge, `gap 14px`.

**1 · Stack-Status** — volle Breite, `border-left: 3px solid` in Zustandsfarbe.
Kopf: „STACK-ZUSTAND“ + Polling-Hinweis. Körper: großer Badge, daneben Headline
(15px/600) und Sub-Zeile (12.5px, `--txt-2`). Bei `fehler` darunter eine
Fehlerbox (`--err-tint` / `--err-line`): fette Zeile „Start fehlgeschlagen —
Container **acoustid-index** unhealthy“, ein `<code>` mit
`healthcheck exit 1 nach 4 Versuchen · /data/index: input/output error`,
dann zwei Links „Ereignis-Log öffnen“ und „Letzte Läufe ansehen“.

Buttons, zustandsabhängig:

| Zustand | sichtbar |
|---|---|
| `schlafend` | **Wecken** (Primary, mit Inline-Zusatz „weckt das Array“ in 10.5px/500, `opacity .72`) |
| `bereit` | **Jetzt schlafen legen** (Standard) + **Wecken** deaktiviert, gestrichelter Rahmen, `title="Der Stack läuft bereits — Wecken ist in diesem Zustand ohne Wirkung."` |
| `fehler` | **Neu starten** (`--err-tint` / `--err-line` / `--err-fg`) |
| `startet`, `stoppt` | keine; stattdessen „Aktionen gesperrt, solange der Übergang läuft.“ |

**2 · Datenstand** — Kopf mit Link „Jobs“. Letzte Delta-Sequenz `#1 742 883`
(17px mono), darunter „24.07.2026, 03:14 · 42 Dateien, 1 284 402 Zeilen“.
Trennlinie. „Nächster Update-Lauf → heute 03:00“. „Letzter Lauf → ● ok · 14 min 22 s“
(`--ok-fg`). Fehlervariante: statt der Zeile eine Box (`--err-tint`) mit
„Letzter Lauf fehlgeschlagen“, „Update 25.07.2026 03:00 · Abbruch nach 2 min 11 s ·
Datenstand unverändert“ und Link „Fehlermeldung in der Historie öffnen“.

**3 · Aktivität** — Kopf mit Link „Statistiken“. Lookups heute `1 284` und
7 Tage `9 137` (je 20px mono). Cache-Hit-Quote `78,4 %` mit 5px-Meter
(`--acc-solid` auf `--line`). Trennlinie. Submissions: lokal `63`,
weitergeleitet `41`, wartend `3` (wartend `--warn-fg` wenn > 0, sonst `--txt`).

**4 · System** — Array frei `412 GB / 3,6 TB` mit Meter, Fußnote „Warnschwelle
500 GB · Mindestreserve 300 GB“; Meter und Wert in `--warn-fg` unter der
Schwelle. Cache-Volume frei `21,8 GB / 40 GB`. Trennlinie. Lookup-Cache
`1,9 GB · 412 883 Einträge`. Version `0.9.4 · Wächter 0.9.4`.

**5 · Letzte Ereignisse** — volle Breite, Kopf mit Link „Alle Ereignisse“, die
letzten 8 Einträge als Zeilen mit fixen Spaltenbreiten: Zeit `84px` (mono,
`--txt-4`), Level `48px` (10px/600, uppercase, Levelfarbe), Quelle `68px` (mono),
Nachricht flexibel. `min-width: 520px`, Wrapper `overflow-x: auto`.

### Konfiguration — `/admin/config` · `07`, `08`, `09`

`max-width 820px`. Einleitung: „Gespeichert wird pro Gruppe — jede Gruppe ist ein
eigenes Formular und schreibt nur ihren Abschnitt in `config.yaml`. Felder mit der
Markierung `API-RELOAD` lösen beim Speichern einen Reload der API-Konfiguration
aus. Das Array wird dabei nicht geweckt.“

**Speicher-Interaktion: pro Gruppe.** Jede Gruppe ist eine Karte und ein eigenes
`<form hx-post="/admin/config/{gruppe}">`. Begründung: kleine Partial-Swaps,
begrenzter Schaden bei Validierungsfehlern, unabhängige Reload-Semantik.

Karten-Kopf: Gruppentitel (13px/600) + Notiz (11.5px, `--txt-4`); rechts bei
Änderungen ein Badge „● ungespeichert“ (`--warn-*`). Körper:
`grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap 14px`.
Fuß (`border-top`): rechtsbündig „Verwerfen“ (nur wenn dirty) und der
Speicher-Button — dirty → Primary mit „Gruppe speichern“, clean → deaktiviert,
transparent, `--txt-4`, Text „Gespeichert“, `cursor: not-allowed`.

Feldaufbau: Label-Zeile (Label + optionaler `API-RELOAD`-Tag: 16px hoch,
1px `--warn-line`, `--warn-fg`, 9.5px/600 uppercase) → Control → optionale
Fehlerzeile (11.5px, `--err-fg`) → optionaler Hilfstext (11.5px, `--txt-4`).

Die acht Gruppen mit exakten Feldern und Hilfstexten:

1. **API & Auth** — „Zugriffsschutz der Lookup-Schnittstelle“
   - Auth-Modus (Select `none` / `apikey`, **API-Reload**). Optionen:
     „none — offen im LAN“, „apikey — Key erforderlich“.
     Hilfe: „Bei ‚apikey‘ prüft die API den Header X-API-Key gegen die Key-Tabelle.“
   - Rate-Limit (Requests/min), **API-Reload**.
     Hilfe: „0 = unbegrenzt. Gilt pro Key, im Modus ‚none‘ pro IP.“
2. **Submit** — „Was mit eingereichten Fingerprints passiert“
   - Submit-Modus (Select, **API-Reload**): „off — Submits ablehnen“,
     „local — nur lokal speichern“, „local+upstream — lokal + weiterleiten“.
     Hilfe: „Weiterleitung läuft über die Upstream-Queue und weckt das Array nicht.“
   - Upstream-App-Key (Secret). Hilfe: „Wird nur beim Weiterleiten an acoustid.org benutzt.“
3. **Betrieb** — „Wann der Stack laufen darf und wann er schlafen geht“
   - Idle-Timeout (min). Hilfe: „Ohne Lookups fährt der Wächter den Stack danach
     herunter.“ Validierung: Ganzzahl 1–1440, sonst Feldrahmen `--err-fg` und
     „Ganzzahl zwischen 1 und 1440 Minuten erwartet.“
   - Weck-Haltezeit (min). Hilfe: „Mindestlaufzeit nach einem Weckvorgang, damit
     das Array nicht taktet.“
   - Update-Uhrzeit. Hilfe: „Täglicher Delta-Lauf. Weckt das Array planmäßig.“
   - Mindest-Plattenreserve (GB). Hilfe: „Unterschreitung stoppt Import und Backup.“
4. **Cache** — „Lookup-Antworten aus dem Wächter — hält das Array im Schlaf“
   - Lookup-Cache (Toggle, Label „aktiv“ / „deaktiviert“).
     Hilfe: „Ohne Cache weckt jeder Lookup das Array.“
   - Maximalgröße (GB)
   - Aktion „Cache jetzt leeren“ (Rahmen `--err-line`, Text `--err-fg`)
5. **Benachrichtigungen** — „Nur bei Fehlern — Erfolge bleiben still“
   - ntfy-/Webhook-URL (volle Zeilenbreite), Aktion „Testnachricht senden“
   - SMTP-Host, SMTP-Port, SMTP-Benutzer, SMTP-Passwort (Secret),
     Absender (From), Empfänger (To), Aktion „Test-Mail senden“
6. **Backup** — „Sichert Index, Datenbank und config.yaml“
   - Zielverzeichnis; Uhrzeit. Hilfe: „Läuft nach dem Update-Lauf, solange das
     Array ohnehin wach ist.“
7. **MusicBrainz** — „Nur für Metadaten-Anreicherung“
   - DSN (Secret, volle Breite). Hilfe: `postgresql://user:passwort@host:5432/musicbrainz`
   - Aktion „Verbindung testen“. Hilfe: „Der Test läuft direkt vom Wächter und
     weckt das Array nicht.“
8. **Admin** — „Zugang zu dieser Oberfläche“
   - Aktion „Passwort ändern…“ (öffnet den Dialog unten).
     Hilfe: „Eigener Dialog mit Bestätigung des aktuellen Passworts — läuft nicht
     über das Speichern dieser Gruppe.“
   - Benutzername, nicht editierbar, Rahmen `--line`.
     Hilfe: „Fest vorgegeben — ein Admin-Benutzer pro Instanz.“

**Secret-Felder** (§5.5): gesetzt → gestrichelter Rahmen, `--bg`-Fläche,
mono `•••••••••••••• gesetzt`, daneben Button „Ändern“. Nach Klick ein leeres
`<input type="password" placeholder="Neuer Wert">` mit Akzentrahmen und
„Abbrechen“. Der Klartext wird **nie** zurückgeliefert.

### Passwort-Dialog · `09-dialog-passwort-aendern.png`

`max-width 436px`. Titel „Admin-Passwort ändern“, Sub „Benutzer **admin** ·
mindestens 12 Zeichen“. Drei Felder à `36px`: Aktuelles Passwort, Neues Passwort,
Neues Passwort wiederholen.

Unter dem neuen Passwort ein 4px-Stärke-Meter plus Textlabel; Score aus fünf
Kriterien (≥12 Zeichen, ≥16 Zeichen, Groß+Klein, Ziffer, Sonderzeichen), Labels
„zu kurz / schwach / brauchbar / gut / stark“, Farbe `--err-fg` (≤1),
`--warn-fg` (2), sonst `--ok-fg`. Leeres Feld → Meter `--line`, Label `—`.

Validierung erst nach dem ersten Submit: „Aktuelles Passwort erforderlich.“,
„Mindestens 12 Zeichen erforderlich.“, „Die beiden Eingaben stimmen nicht überein.“

Warnbox: „Nach dem Ändern werden alle bestehenden Sessions ungültig — auch diese.
Du wirst neu angemeldet.“ Erfolg → Redirect auf Login + Toast
„Passwort geändert — bitte neu anmelden.“

### API-Keys — `/admin/keys` · `11`, `12`

`max-width 1000px`.

Im Auth-Modus `none` oben eine Info-Box (`--acc-tint` / `--acc-line`, Punkt
`--acc-fg`): „Auth-Modus steht auf `none` — Keys werden derzeit nicht geprüft. Du
kannst sie trotzdem anlegen und vorbereiten; sie greifen, sobald du in der
[Konfiguration] auf `apikey` umstellst.“

Karten-Kopf „KEYS ({n})“ + Primary „Neuen Key erzeugen“.

Tabelle, `min-width 760px` im `overflow-x: auto`-Wrapper. Spalten: Label
(13px/500) · Key (mono 12px, `--txt-2`) · Status · Erstellt (mono) · Zuletzt
benutzt (mono) · Aktionen (rechts). Zeilen `padding 11px`, Trennung
`1px solid --line`.
Status-Badge `21px`: aktiv → `--ok-*`; inaktiv → neutral (`--txt-3`,
`--surface-2`, `--line-2`).
Aktionen: „Deaktivieren“/„Aktivieren“ (`28px`, transparent) und „Löschen“
(Rahmen und Text in Fehlerfarbe).

**Einmalige Klartext-Anzeige** nach dem Erzeugen: Box in `--ok-tint` /
`--ok-line`, Titel „Key ‚{label}‘ erzeugt“, Sub „Einmalige Anzeige — nach dem
Verlassen der Seite ist der Key nur noch maskiert sichtbar.“, dann der Key als
`<code>` (mono, `--bg`, nicht umbrechend, horizontal scrollbar) mit den Buttons
„Kopieren“ und „Verstanden“.

**Leerzustand** (`12-api-keys-leer.png`): `44px 20px` Padding, zentriert —
ein 38px-Platzhalterquadrat (gestrichelter Rahmen,
`repeating-linear-gradient(135deg, var(--surface-2) 0 5px, var(--surface) 5px 10px)`),
„Noch keine API-Keys“, „Lege einen Key pro Client an (Navidrome, beets, Skript) —
so kannst du einzelne Clients später sperren, ohne alle anderen zu stören.“, Button
„Ersten Key erzeugen“.

**Erzeugen-Dialog:** `max-width 420px`, Titel „Neuen API-Key erzeugen“, Sub „Der
Key wird genau einmal im Klartext angezeigt.“, ein Feld „Label“
(Platzhalter „z. B. Navidrome“), Hilfe „Nur zur Wiedererkennung in dieser Tabelle
— der Client sieht das Label nicht.“, Buttons „Abbrechen“ / „Key erzeugen“.

### Updates & Jobs — `/admin/jobs` · `14`, `15`, `16`, `17`, `18`

`max-width 1060px`.

**Manuelle Aktionen** — drei Kacheln (`--bg`-Fläche, 1px `--line`, `13px` Padding):
Titel, Beschreibung, unten Button + Tag `WECKT DAS ARRAY` (20px,
`--warn-line`/`--warn-fg`, 10px/600 uppercase).

| Kachel | Text | Button | weckt |
|---|---|---|---|
| Update jetzt ausführen | „Lädt fehlende Delta-Sequenzen und spielt sie in den Index ein.“ | Update starten | ja |
| Backup jetzt ausführen | „Sichert Index, Datenbank und config.yaml nach /mnt/backup.“ | Backup starten | ja |
| Upstream-Queue jetzt senden | „{N} Submissions warten auf die Weiterleitung an acoustid.org.“ / bei N=0 „Die Queue ist leer — nichts zu senden.“ | Queue senden | nein |

Deaktivierung (§5.2): während eines laufenden Jobs alle drei deaktiviert,
`title="Es läuft bereits ein Job — bitte abwarten oder abbrechen."`; Queue-Button
bei `N = 0` deaktiviert mit `title="Die Upstream-Queue ist leer."`.

**Laufender Job** — Panel `--acc-tint` / `--acc-line`: pulsender Punkt, Titel
„{Typ}-Lauf läuft“, „gestartet 05:41 · Polling 5 s“, rechts „Abbrechen“
(Fehlerfarbe). Darunter Schrittname + Prozent (mono/600), ein 7px-Balken in
`--acc-solid` mit diagonalem Streifen-Overlay
(`linear-gradient(115deg, oklch(1 0 0 / .16) 25%, transparent 25% 50%, oklch(1 0 0 / .16) 50% 75%, transparent 75%)`,
`background-size: 22px 22px`, `animation: ao-stripe .9s linear infinite`) und
eine Detailzeile in mono, z. B. „Datei 17 von 42 · Sequenz #1 742 884 ·
318 402 Zeilen“. Schrittnamen: Update → „Delta-Sequenzen einspielen“,
Backup → „Archiv schreiben“, Queue → „Submissions senden“.

**Historie** — Kopf mit zwei Selects (`30px`): „Typ: alle / Update / Backup“ und
„Ergebnis: alle / ok / fehlgeschlagen“. Tabelle `min-width 820px`: Typ · Start ·
Dauer · Dateien/Zeilen · Ergebnis (● ok in `--ok-fg` / ● fehlgeschlagen in
`--err-fg`) · rechts bei Fehlern ein `26px`-Button „Fehler anzeigen“ /
„Fehler ausblenden“. Aufgeklappt: zusätzliche Zeile (`colspan 6`, Fläche `--bg`)
mit `<pre>` in `--err-tint` / `--err-line`, mono 11.5px, `line-height 1.6`,
`white-space: pre-wrap` — mehrzeilige Fehlerdetails inklusive Retry-Backoffs und
der Aussage, dass der Datenstand unverändert blieb.

**Leerzustand** — zwei Varianten: gefiltert → „Keine Läufe für diesen Filter“ /
„Setze Typ oder Ergebnis zurück, um alle Läufe zu sehen.“; ungefiltert →
„Noch keine Läufe aufgezeichnet“ / „Der erste geplante Update-Lauf startet heute
um 03:00. Bis dahin bleibt die Historie leer — das ist kein Fehler.“

### Ereignis-Log — `/admin/logs` · `20`, `21`

`max-width 1120px`. Filterkopf, `flex-wrap`:

- Level-Chips „Alle / Info / Warn / Error“, je `28px`, mit farbigem Punkt und
  Trefferzahl in mono (`opacity .6`). Aktiv → `--surface-2`, Rahmen heller,
  `--txt`.
- Select „Quelle: alle / Wächter / API / Importer“
- Select „Zeitraum: 24 Std. / 7 Tage / 30 Tage / alles“ (Default 30 Tage)
- Freitextfeld `190px`, Platzhalter „Freitext filtern…“
- Rechts ein Auto-Refresh-Schalter: 26×15px-Track + 11px-Knopf, Label
  „Auto-Refresh an/aus“

Zeilen (`min-width 640px`): Zeit `116px` mono `--txt-4` · Level-Badge `54px`
(18px hoch, zentriert, 9.5px/600 uppercase, Levelfarben; Info neutral) · Quelle
`72px` mono · Nachricht flexibel. Error-Zeilen bekommen zusätzlich eine leicht
rote Zeilenfläche.

Fuß (`border-top`): links „Einträge 1–10 von 24“ (mono), Mitte Pagination
„Neuer“ · „Seite 1 / 3“ · „Älter“ (Grenzen deaktiviert, `--txt-4`,
`cursor: not-allowed`), rechts „Nur strukturierte Events aus `event_log` — kein
Container-stdout.“ (auf Mobil ausgeblendet). **10 Einträge pro Seite**; jeder
Filterwechsel setzt auf Seite 1 zurück.

Leerzustand: gefiltert → „Keine Ereignisse für diesen Filter“ / „Kein Eintrag
passt auf Level, Quelle und Suchtext.“ + Button „Filter zurücksetzen“;
ungefiltert → „Noch keine Ereignisse“ / „Der Wächter schreibt Ereignisse, sobald
er startet, weckt oder importiert.“

### Statistiken — `/admin/stats` · `23`

`max-width 1120px`. Fünf KPI-Kacheln (`minmax(178px, 1fr)`): Label uppercase
10.5px, Wert 21px mono/600, Sub 11.5px `--txt-4`.

| Label | Wert | Sub |
|---|---|---|
| Tracks gesamt | 34 812 004 | Stand 25.07.2026 |
| Fingerprints | 71 204 883 | Index 41,2 GB |
| Submissions lokal | 63 | 41 weitergeleitet |
| Datenstand | #1 742 883 | 24.07.2026, 03:14 |
| Wachzeit 7 Tage | 4 h 51 m | 2,9 % der Woche |

Fünf Diagramme (`minmax(340px, 1fr)`): Lookups pro Tag (Balken) ·
Cache-Hit-Quote (Linie, Skala 60–90 %) · Weckvorgänge pro Tag (Balken, neutrale
Farbe `--txt-1` — Wecken ist kein Erfolg) · Import-Dauer je Lauf (Balken,
abgebrochene Läufe in `--err-fg`) · DB- und Index-Größe (Linie mit Fläche,
Skala 55–66 GB).

Karten-Kopf: Titel uppercase + Zeitraum in mono rechts. Körper: aktueller Wert
(17px mono) + Notiz, dann das SVG, rechts oben der Maximalwert (10px mono),
darunter drei X-Labels (erster / mittlerer / letzter Tag).

**Chart-Lösung: serverseitig gerendertes Inline-SVG, keine JS-Bibliothek.**
Jinja2 rechnet Balken-Rechtecke und Pfade; HTMX kann einzelne Diagramme als
Partial nachladen. Kein Build, kein CDN, funktioniert offline — genau der
Constraint aus §1.

SVG-Geometrie: `viewBox="0 0 680 132"`, `preserveAspectRatio="none"`,
`width: 100%; height: 118px`, `overflow: visible`. Drei Hilfslinien: oben
`y=0.5` (`--line`), Mitte `y=66` gestrichelt `3 4` (`--surface-2`), unten
`y=131.5` (`--line-2`). Balken: `gap 5`, Breite `(680 − (n−1)·5) / n`,
Höhe `max(2, v/max · 128)`, `rx 1`. Linien: `stroke-width 1.75`,
`vector-effect="non-scaling-stroke"` (sonst verzerrt `preserveAspectRatio="none"`
die Linienstärke), `stroke-linejoin/linecap: round`, Flächenfüllung als
`{Farbe} / 0.13`.

Fußnote: „Alle Werte stammen aus den Aufzeichnungen des Wächters (SQLite). DB-
und Index-Größen werden beim Update-Lauf erfasst — es gibt keinen Live-Zugriff auf
das Array. Die Diagramme sind serverseitig als SVG gerendert; kein JavaScript,
kein Build-Schritt.“

---

## Bestätigungsdialoge

Ein Bauteil für alle. Overlay `--scrim` + `blur(2px)`, Karte `max-width 452px`,
`border-radius 5px`, `animation: ao-fade .14s ease-out`, `role="dialog"`,
`aria-modal="true"`.

Aufbau: Kopf (Titel 14.5px/600 + Body 12.5px `--txt-2`, `border-bottom`) →
optionale Warnbox (`--warn-tint` / `--warn-line`, Punkt + Text) → optionaler
`<code>`-Block für Details → Fuß rechtsbündig „Abbrechen“ (transparent) +
Bestätigung. Bestätigung ist Primary, bei destruktiven Aktionen
`--err-solid` / `--on-err`.

| Auslöser | Titel | Body | Warnbox | Bestätigung |
|---|---|---|---|---|
| Wecken | Stack wecken? | „Der Wächter fährt Array und Container hoch. Bis Lookups direkt beantwortet werden, vergehen etwa 40 Sekunden.“ | „Diese Aktion weckt das Array. Es bleibt mindestens 10 Minuten wach (Weck-Haltezeit).“ | Wecken |
| Schlafen legen | Stack jetzt schlafen legen? | „Alle Container werden gestoppt, danach darf das Array herunterfahren. Laufende Lookups werden zu Ende beantwortet.“ | „Der nächste Cache-Miss weckt das Array erneut — mit rund 40 Sekunden Verzögerung.“ | Schlafen legen |
| Neu starten | Stack neu starten? | „Alle Container werden hart gestoppt und neu gestartet. Der letzte Start ist mit einem I/O-Fehler auf /data/index gescheitert.“ | „Weckt das Array und stoppt den Stack. Bei erneutem Fehlschlag bleibt der Zustand ‚Fehler‘.“ | Neu starten *(destruktiv)* |
| Update jetzt | Update jetzt ausführen? | „Der Wächter startet den Stack und spielt alle fehlenden Delta-Sequenzen ein. Der Lauf dauert erfahrungsgemäß 12–15 Minuten.“ | „Diese Aktion weckt das Array…“ | Update starten |
| Backup jetzt | Backup jetzt ausführen? | „Der Wächter startet den Stack, hält die Datenbank kurz an und schreibt ein konsistentes Archiv nach /mnt/backup.“ | „Diese Aktion weckt das Array…“ | Backup starten |
| Queue senden | Upstream-Queue jetzt senden? | „Die {N} wartenden Submissions werden direkt vom Wächter an acoustid.org gesendet.“ | — | Queue senden |
| Job abbrechen | Laufenden Job abbrechen? | „Der Lauf wird an der nächsten sicheren Stelle gestoppt. Bereits eingespielte Dateien bleiben erhalten, der Datenstand bleibt konsistent.“ | „Der Stack bleibt bis zum Ende der Weck-Haltezeit wach.“ | Job abbrechen *(destruktiv)* |
| Cache leeren | Lookup-Cache leeren? | „Alle 412 883 zwischengespeicherten Antworten werden verworfen.“ | „Danach wecken die nächsten Lookups das Array, bis der Cache wieder gefüllt ist.“ | Cache leeren *(destruktiv)* |
| Key deaktivieren | Key „{label}“ deaktivieren? | „Der Client kann sofort keine Lookups mehr ausführen. Der Key bleibt erhalten und kann wieder aktiviert werden.“ | — | Deaktivieren |
| Key löschen | Key „{label}“ löschen? | „Der Key wird unwiderruflich entfernt. Clients mit diesem Key erhalten ab sofort HTTP 401.“ | — (dafür `<code>` mit dem maskierten Key) | Endgültig löschen *(destruktiv)* |
| Abmelden | Abmelden? | „Die Session wird beendet. Laufende Jobs des Wächters laufen unabhängig davon weiter.“ | — | Abmelden |

Key aktivieren braucht **keine** Bestätigung — die Aktion ist nicht destruktiv.

## Toast

„Fehler sind laut, Erfolg ist leise“ (§5.4): Erfolge nur als Toast unten links,
`left/bottom 20px`, `z-index 70`, `--surface-2`, 1px `--line-2`,
`border-radius 3px`, `padding 9px 13px`, `ao-fade`, grüner 5px-Punkt +
12.5px-Text, **3,4 s** Anzeigedauer, kein Schließen-Button.

Texte: „{Gruppe} gespeichert.“ · „Lookup-Cache geleert.“ · „Testnachricht an ntfy
gesendet.“ · „Test-Mail an admin@example.net gesendet.“ · „MusicBrainz-Verbindung
ok (42 ms).“ · „Key in die Zwischenablage kopiert.“ · „Key aktiviert.“ /
„Key deaktiviert.“ / „Key gelöscht.“ · „Weckvorgang gestartet.“ · „Stack wird
heruntergefahren.“ · „Neustart angestoßen.“ · „Job abgebrochen.“ ·
„Passwort geändert — bitte neu anmelden.“

---

## Interactions & Behavior

### HTMX-Polling (§5.3, sparsam)

| Ziel | Intervall | Bedingung |
|---|---|---|
| Stack-Status-Karte + Header-Badge + Neben-Badges | 5 s | immer, auf jeder Seite |
| Job-Fortschrittspanel | 5 s | nur bei laufendem Job |
| Log-Liste | 5 s | nur wenn Auto-Refresh an |
| Konfiguration, Keys, Historie, Statistiken | — | kein Polling |

Der Header zeigt „aktualisiert vor {n} s · Polling 5 s“ bzw. auf statischen
Seiten „statische Seite · kein Polling“. Der Prototyp macht das mit einem
1-Sekunden-Ticker; in HTMX ist es `hx-trigger="every 5s"` auf dem Statuspartial.

**Kein Polling-Request darf das Array wecken.** Alle Daten kommen aus
Wächter-SQLite und `config.yaml`.

### Zustandsübergänge

- Wecken bestätigt → `startet`, nach erfolgreichem Hochfahren → `bereit`
  (im Prototyp ~6 s Platzhalter; real ~40 s)
- Schlafen legen bestätigt → `stoppt`, dann → `schlafend` (~4 s Platzhalter)
- Neu starten bestätigt → `startet` → `bereit` oder zurück zu `fehler`
- Idle-Timeout erreicht → `bereit` → `stoppt` → `schlafend` (serverseitig)
- Weckende Job-Aktion → parallel `startet`, Job-Panel erscheint sofort

### Formulare

- Validierung inline unter dem Feld, Feldrahmen `--err-fg`
- Der Passwort-Dialog validiert erst nach dem ersten Submit-Versuch
- Speichern pro Gruppe; „Verwerfen“ setzt die Gruppe auf den Serverstand zurück
- Erfolg → Toast, kein Full-Page-Reload

### Responsive

Desktop primär, muss auf Tablet und Smartphone benutzbar bleiben — eine UI, keine
separate Mobile-Variante.

Intrinsisch responsiv über `auto-fit`-Grids, `flex-wrap` und `max-width` — die
Kartenraster brauchen keinen Breakpoint. Zusätzlich **ein** Breakpoint:

```css
@media (max-width: 900px) {
  /* Shell von row auf column */
  /* Sidebar volle Breite, Rahmen rechts → unten */
  /* Nav-Liste horizontal, scrollbar, aktives Item mit unterem statt linkem Rahmen */
  /* Route-Zeile und Log-Fußnote ausblenden */
  /* alle Tap-Ziele min-height: 44px */
}
```

Tabellen liegen **immer** (nicht erst unter 900px) in einem Wrapper mit
`overflow-x: auto; min-width: 0`; die Tabellen selbst haben `min-width`
(760–820px). Ohne das bricht die Aktionsspalte aus der Karte und die ganze Seite
scrollt horizontal.

Reihenfolge der Sidebar-Elemente bleibt auf Mobil erhalten; der Stack-Badge sitzt
im Header und ist damit auch mobil immer sichtbar.

---

## State Management (serverseitig aufzulösen)

| State | Werte | Quelle |
|---|---|---|
| `stack_state` | schlafend / startet / bereit / stoppt / fehler | Wächter |
| `stack_since`, `uptime`, `idle_remaining` | Zeitangaben | Wächter |
| `import_running`, `import_file`, `import_total` | bool, int, int | Importer |
| `backup_running` | bool | Wächter |
| `upstream_queue_count` | int (Badge nur > 0) | SQLite |
| `mb_reachable` | bool | letzter Verbindungstest |
| `disk_free`, `disk_warn_threshold` | Bytes | Wächter |
| `last_run` | Typ, Start, Dauer, Ergebnis, Fehlertext | `update_run` |
| `next_run_at` | Zeit | Scheduler |
| `job_running` | Typ, Start, Prozent, Schritt, Detail | Wächter |
| `auth_mode` | none / apikey | `config.yaml` |
| `keys[]` | Label, Maske, aktiv, erstellt, zuletzt benutzt | SQLite |
| `new_key_plaintext` | einmalig, nur in der Response nach dem Erzeugen | flüchtig |
| `config[gruppe]` + `dirty` | Formularwerte, Dirty pro Gruppe | `config.yaml` + Client |
| `secret_set[feld]` | bool (nie der Wert) | `config.yaml` |
| `log_filter` | level, source, window, query, page, auto_refresh | Query-Parameter |
| `job_filter` | type, result | Query-Parameter |
| `theme` | dark / light / system | User-Präferenz |

Dirty-Tracking pro Gruppe ist der einzige echte Client-State — ein kleines
`input`-Listener-Snippet, das den Speicher-Button aktiviert. Alles andere kommt
vom Server.

Filter und Pagination gehören in die URL (`?level=error&window=7&page=2`), damit
HTMX-Swaps und Reloads denselben Stand ergeben.

---

## Assets

Keine. Keine Bilder, keine Icon-Fonts, keine Webfonts, keine
Illustrations-SVGs — nur Text, CSS-Formen (Punkte, Meter, Toggles) und die
Chart-SVGs aus echten Daten. Der Leerzustands-Platzhalter ist ein
`repeating-linear-gradient` auf einem 38px-Quadrat.

Das ist Absicht: Die UI läuft in einem Container ohne garantierten
Internet-Zugang und soll ohne externe Ressourcen vollständig funktionieren.

## Files

```
design_handoff_acoustid_admin/
├── README.md                              ← dieses Dokument
├── DESIGN_HANDOFF_original.md             ← fachliche Ausgangsspezifikation
├── prototype/
│   ├── acoustid-offline-admin.dc.html     ← Design-Referenz, im Browser öffnen
│   └── support.js                         ← Runtime des Prototyps (muss daneben liegen)
└── screenshots/
    ├── 01-dashboard-schlafend.png            Normalzustand
    ├── 02-dashboard-bereit.png               Stack läuft, „Wecken“ deaktiviert
    ├── 03-dashboard-fehler.png               Fehlerbox + „Neu starten“
    ├── 04-dashboard-startet-import-laeuft.png  Übergang + Neben-Badges + Fehler-Datenstand
    ├── 05-dialog-wecken.png                  Bestätigung „weckt das Array“
    ├── 07-konfiguration-oben.png             API & Auth, Submit, Betrieb
    ├── 08-konfiguration-unten.png            Benachrichtigungen bis Admin
    ├── 09-dialog-passwort-aendern.png        Passwort-Dialog mit Stärke-Meter
    ├── 11-api-keys.png                       Tabelle mit drei Keys
    ├── 12-api-keys-leer.png                  Leerzustand
    ├── 14-jobs-aktionen-historie.png         Aktionen + Historie mit Fehlerzeile
    ├── 15-dialog-update-weckt-array.png      Bestätigung Update
    ├── 16-jobs-lauf-mit-fortschritt.png      Laufender Job, Aktionen deaktiviert
    ├── 17-dialog-job-abbrechen.png           destruktive Bestätigung
    ├── 18-jobs-historie-leer.png             Leerzustand Historie
    ├── 20-ereignis-log.png                   Filter, Level-Badges, Pagination
    ├── 21-ereignis-log-leer.png              Leerzustand Log
    ├── 23-statistiken.png                    KPI-Kacheln + SVG-Charts
    ├── 24-login-erststart.png                Hinweis auf Erst-Passwort im Container-Log
    ├── 25-login-fehlversuch.png              Fehlermeldung
    ├── 27-light-dashboard.png                Light-Modus
    ├── 29-light-jobs.png                     Light-Modus
    └── 31-light-statistiken.png              Light-Modus, Charts
```

Screenshots sind bei ~914px Breite aufgenommen — nah am Breakpoint, deshalb ist
die zweispaltige Kartenanordnung zu sehen. Auf breiten Desktops fächern die
`auto-fit`-Grids auf drei bis vier Spalten auf.

## Nicht implementieren

- Die **Prototyp-Steuerung** unten rechts (gestrichelter Rahmen) — reines
  Abnahme-Werkzeug.
- Die Client-State-Logik des Prototyps.
- Der Fortschritt im Prototyp läuft auf einem Timer; real kommt er aus dem
  Job-Status.

## Offene Punkte

- Pagination des Ereignis-Logs ist als „Neuer / Älter“ mit Seitenzahl entworfen.
  Bei sehr großen Logs wäre Cursor-Pagination (`before_id`) sinnvoller als
  `OFFSET` — das ist eine Backend-Entscheidung, die UI bleibt gleich.
- Die Aktivitäts-Karte zeigt Submissions auch im Submit-Modus `off`. Ob die Zeile
  dann entfallen soll, ist noch nicht entschieden.
