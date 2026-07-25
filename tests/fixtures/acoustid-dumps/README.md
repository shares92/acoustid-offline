# Test-Fixtures: AcoustID-Dump-Dateien

Dieses Verzeichnis enthält (lokal, **nicht im Repo**) zehn Original-Tagesdateien
der öffentlichen AcoustID-Delta-Exporte. Sie sind die Referenz für Parser-,
Import- und Schema-Tests: an ihnen wurde in Phase 0 das Delta-Format verifiziert
(ARCHITECTURE.md §5.1), in Phase 6 der Parser.

## Warum die Dateien nicht committet werden

- **Lizenz:** Der AcoustID-Datenbestand steht unter
  [CC BY-SA 3.0 Unported](https://creativecommons.org/licenses/by-sa/3.0/)
  (das MusicBrainz-AcoustID-Mapping ist Public Domain / CC0). Dieses Repo
  verteilt den Datenbestand bewusst **nicht weiter** — Weiterverteilung ist
  laut ARCHITECTURE.md §11 ausdrücklich ausgeschlossen und bleibt Sache der
  jeweiligen Instanz-Betreiber.
- **Größe:** Allein die Fingerprint-Datei eines einzigen Tages ist ~8,5 MB gz;
  Binärdaten gehören nicht in die Repo-Historie.

`.gitignore` schließt daher `tests/fixtures/acoustid-dumps/*.jsonl.gz` aus.
Diese README und das Skript bleiben versioniert, damit die Fixtures jederzeit
reproduzierbar beschafft werden können.

## Beschaffung

```bash
uv run python tests/fixtures/fetch_fixtures.py
# oder ohne uv:
python3 tests/fixtures/fetch_fixtures.py
```

Das Skript (nur Python-Stdlib) lädt genau die unten gelisteten Dateien von
`https://data.acoustid.org/<YYYY>/<YYYY-MM>/`, überspringt bereits vorhandene
Dateien und prüft Größe und gzip-Integrität. Optionen: `--force` (erneut
laden), `--dest DIR` (anderes Zielverzeichnis), `--list` (nur auflisten).

## Inhalt

| Datei | Größe (Byte) | Zeilen | Zweck |
|---|---|---|---|
| `2011-08-19-meta-update.jsonl.gz` | 423.795 | 11.677 | Erster Tag der Historie und einziger Beleg der **Alt-Epoche**: bis 2024-12-04 liegt über dem JSON zusätzlich das Text-Escaping von `COPY` — 85 Zeilen sind ohne Unescape kein gültiges JSON (Phase 6) |
| `2026-07-22-fingerprint-update.jsonl.gz` | 8.925.088 | 2.214 | Fingerprint-Vektoren (int32-Arrays), größter Strom |
| `2026-07-22-meta-update.jsonl.gz` | 169.027 | 5.122 | Metadaten inkl. Sonderzeichen/Quotes (JSONL-Validität belegt), 765 Zeilen mit CJK-Titeln |
| `2026-07-22-track-update.jsonl.gz` | 53.159 | 1.693 | Tracks (in diesem Tag ohne `new_id`/`updated`) |
| `2026-07-22-track_fingerprint-update.jsonl.gz` | 35.820 | 2.214 | zweite Projektion derselben Upstream-Tabelle |
| `2026-07-22-track_mbid-update.jsonl.gz` | 37.387 | 1.039 | Track→MBID-Zuordnungen, 9 Zeilen `disabled: true` |
| `2026-07-22-track_meta-update.jsonl.gz` | 122.663 | 7.141 | Track→Meta-Zuordnungen |
| `2026-07-22-track_puid-update.jsonl.gz` | 23 | 0 | Edge Case: legitime **leere** Datei (Legacy-Strom) |
| `2026-07-23-fingerprint-update.jsonl.gz` | 23 | 0 | Edge Case: leerer Tag im größten Strom (Daten-Flaute) |
| `2026-07-23-track_mbid-update.jsonl.gz` | 316 | 4 | Edge Case: Minimal-Delta, 2× `disabled: true`, alle 4 Zeilen mit `updated` |

Der 22.07.2026 ist als vollständiger Tag (alle sieben Ströme) enthalten, der
23.07.2026 liefert die Randfälle, der 19.08.2011 die Alt-Epoche. Die Dateien
sind upstream unveränderlich; Größen und Zeilenzahlen sind damit stabile
Erwartungswerte (die Parser-Tests in `importer/tests/` prüfen gegen sie).

## Quelle und Nutzungshinweis

Quelle: <https://data.acoustid.org/> (AcoustID OÜ). Beim Nachladen bitte
sparsam bleiben — die Dateien einmal holen und liegen lassen, nicht in
Schleifen ziehen.
