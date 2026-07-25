# DESIGN_HANDOFF.md — Admin-UI acoustid-offline

Input für die Claude-Design-Session. Beschreibt Screens, Komponenten,
Zustände und Interaktionen der Admin-UI. Technische Rahmenbedingungen
sind fix (siehe §1); das visuelle Design ist Gegenstand der
Design-Session.

Referenz: HANDOFF.md (Gesamtspezifikation).

---

## 1. Technischer Rahmen (fix, nicht verhandelbar)

- Server-rendered: FastAPI + Jinja2-Templates + HTMX. Kein
  Frontend-Build, kein SPA-Framework, kein npm.
- CSS: eine handgeschriebene Stylesheet-Datei oder klassenloses/leichtes
  CSS-Framework — Entscheidung der Design-Session, aber ohne Build-Schritt.
- Läuft im Wächter-Container unter `/admin`, ein Admin-Benutzer,
  Passwort-Login, Session-Cookie.
- Zielgeräte: Desktop-Browser primär, muss auf Tablet/Smartphone
  benutzbar bleiben (responsive, keine separate Mobile-UI).
- Dynamik über HTMX: Partial-Updates und Polling (z. B. Statuskarte alle
  5 s), keine WebSockets erforderlich.
- Kein UI-Aufruf darf das Array wecken — alle Daten kommen aus
  Wächter-SQLite und config.yaml. Array-Aktionen nur nach explizitem
  Klick (Weck-Button, Update-Button).

## 2. Informationsarchitektur

```
/admin/login
/admin/            Dashboard (Status)
/admin/config      Konfiguration
/admin/keys        API-Keys
/admin/jobs        Updates & Jobs (Historie + manuelle Aktionen)
/admin/logs        Ereignis-Log
/admin/stats       Statistiken
```

Persistente Navigation (Sidebar oder Topbar — Design-Entscheidung) mit
den sechs Bereichen + Logout. Auf jeder Seite sichtbar: kompakter
Stack-Zustands-Indikator (siehe §3).

## 3. Zentrale Zustände

Der Stack-Zustand ist das wichtigste UI-Element und muss überall
ablesbar sein:

| Zustand | Bedeutung | Farbe (semantisch) |
|---|---|---|
| `schlafend` | Stack aus, Array darf schlafen — Normalzustand | neutral/grau |
| `startet` | Wächter fährt Stack hoch | Übergang/animiert |
| `bereit` | Stack läuft, Lookups gehen direkt | positiv/grün |
| `stoppt` | Idle-Timeout erreicht, fährt herunter | Übergang |
| `fehler` | Start fehlgeschlagen / Container unhealthy | negativ/rot |

Zusätzliche, parallel anzeigbare Badges:
- `Import läuft` (mit Fortschritt: Datei x von y)
- `Backup läuft`
- `Upstream-Queue: N wartend` (nur wenn N > 0)
- `MB nicht erreichbar — degradierter Betrieb`
- `Plattenplatz knapp`

Wichtig fürs Design: `schlafend` ist ein **guter** Zustand (Ziel des
Systems), darf nicht wie ein Fehler wirken.

## 4. Screens

### 4.1 Login (`/admin/login`)
- Ein Feld Passwort (Benutzername fix), Fehlermeldung bei Fehlversuch,
  Rate-Limit-Hinweis nach mehreren Fehlversuchen.
- Hinweistext beim allerersten Start: Erst-Passwort steht im
  Container-Log.

### 4.2 Dashboard (`/admin/`)
Karten-Layout, wichtigste Ebene zuerst:
1. **Stack-Status-Karte:** Zustand (§3), Uptime bzw. "schläft seit",
   Buttons: `Wecken` (nur wenn schlafend), `Jetzt schlafen legen`
   (nur wenn bereit + idle), `Neu starten` (nur bei Fehler).
   Buttons mit Bestätigungsdialog.
2. **Datenstand-Karte:** letzte eingespielte Delta-Sequenz + Datum,
   nächster geplanter Update-Lauf, Ergebnis des letzten Laufs
   (ok/fehlgeschlagen mit Link zu Jobs).
3. **Aktivitäts-Karte:** Lookups heute / 7 Tage, Cache-Hit-Quote,
   Submissions lokal / weitergeleitet / wartend.
4. **System-Karte:** freier Platz Array + Cache (mit Warnschwelle),
   Lookup-Cache-Größe, Version der Instanz.
5. **Letzte Ereignisse:** die letzten ~10 Log-Einträge, Link zu Logs.

### 4.3 Konfiguration (`/admin/config`)
Formular über die Werte aus HANDOFF.md §6, gruppiert:
- **API & Auth:** Auth-Modus (none/apikey), Rate-Limit.
- **Submit:** Modus (off/local/local+upstream), Upstream-App-Key
  (Secret-Feld, maskiert).
- **Betrieb:** Idle-Timeout, Weck-Haltezeit, Update-Uhrzeit,
  Mindest-Plattenreserve.
- **Cache:** an/aus, Maximalgröße, Button "Cache jetzt leeren".
- **Benachrichtigungen:** ntfy/Webhook-URL, SMTP-Block (Host, Port,
  User, Passwort, From, To), je ein "Testnachricht senden"-Button.
- **Backup:** Zielverzeichnis, Uhrzeit.
- **MusicBrainz:** DSN (Secret-Feld), Button "Verbindung testen"
  (Hinweis: weckt das Array nicht — Test läuft direkt vom Wächter).
- **Admin:** Passwort ändern.

Verhalten: Speichern pro Gruppe oder global (Design-Entscheidung),
Validierungsfehler inline, Erfolgsbestätigung unaufdringlich.
Änderungen, die einen API-Reload auslösen, werden als solche markiert.

### 4.4 API-Keys (`/admin/keys`)
Nur relevant im Modus `apikey`; im Modus `none` mit Hinweis trotzdem
bedienbar (Vorbereitung).
- Tabelle: Label, Key (nur bei Erstellung einmal im Klartext sichtbar,
  danach maskiert), aktiv/inaktiv, erstellt, zuletzt benutzt.
- Aktionen: neuen Key erzeugen (Dialog mit Label), deaktivieren/
  aktivieren, löschen (Bestätigung), Key kopieren (nur direkt nach
  Erstellung).

### 4.5 Updates & Jobs (`/admin/jobs`)
- **Aktionen oben:** `Update jetzt ausführen`, `Backup jetzt ausführen`,
  `Upstream-Queue jetzt senden` — jeweils mit Hinweis "weckt das Array"
  und Bestätigung. Bei laufendem Job: Fortschrittsanzeige
  (HTMX-Polling), Abbrechen-Button.
- **Historie:** Tabelle der Läufe (`update_run`): Typ (Update/Backup),
  Start, Dauer, eingespielte Dateien/Zeilen, Ergebnis, aufklappbare
  Fehlermeldung. Filter nach Typ und Ergebnis.

### 4.6 Ereignis-Log (`/admin/logs`)
- Tabelle/Liste aus `event_log`: Zeit, Level (info/warn/error),
  Quelle (Wächter/API/Importer), Nachricht.
- Filter: Level, Quelle, Freitext. Auto-Refresh per HTMX umschaltbar.
- Kein Zugriff auf Container-Stdout nötig — nur strukturierte Events.

### 4.7 Statistiken (`/admin/stats`)
- Zeitreihen (einfache Charts, serverseitig gerendert oder leichte
  JS-Chart-Lib ohne Build — Design-Entscheidung): Lookups/Tag,
  Cache-Hit-Quote, Weckvorgänge/Tag, Import-Dauer je Lauf,
  DB-/Index-Größe über Zeit.
- Kennzahlen-Kacheln: Gesamt-Tracks, Gesamt-Fingerprints, lokale
  Submissions, Datenstand.
- Hinweis: Werte stammen aus Wächter-Aufzeichnungen; DB-Größen werden
  beim Update-Lauf erfasst (kein Live-Zugriff auf das Array).

## 5. Interaktionsprinzipien

1. **Destruktive/weckende Aktionen immer mit Bestätigung** und klarer
   Kennzeichnung ("weckt das Array", "stoppt den Stack").
2. **Zustandsabhängige Buttons:** Aktionen, die im aktuellen Zustand
   sinnlos sind, werden ausgeblendet oder deaktiviert mit Tooltip.
3. **Polling sparsam:** Statuskarte und laufende Jobs pollen (5 s),
   statische Seiten nicht.
4. **Fehler sind laut, Erfolg ist leise:** Fehlgeschlagene Läufe und
   Fehlerzustände prominent (Dashboard-Karte + Badge), Erfolge nur als
   dezente Bestätigung.
5. **Secrets nie im Klartext** nach dem Speichern (maskierte Felder,
   "ändern"-Interaktion statt Anzeige).
6. **Deutsch als UI-Sprache**, technische Begriffe (Lookup, Submit,
   Delta) bleiben englisch.

## 6. Offen für die Design-Session

- Visuelle Richtung (Farbwelt, Typo, Dichte), Dark/Light oder nur ein
  Modus.
- Navigation: Sidebar vs. Topbar.
- Chart-Lösung für §4.7 (Constraint: kein Build-Schritt).
- Speicher-Interaktion in der Konfiguration (pro Gruppe vs. global).
- Ausgestaltung der Zustands-Badges (§3) und der Fortschrittsanzeige.
